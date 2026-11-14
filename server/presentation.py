"""Seven-beat pitch data and a request-scoped streaming race for Vercel."""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request

from so import config
from so.contracts import Event, Question, Results


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def first_search_misses(root: Path, matrix_path: Path, engine: str) -> dict | None:
    """Report observed, non-trap, held-out rows, with their actual denominator."""
    try:
        questions = [json.loads(line) for line in (root / 'data/questions.jsonl').read_text().splitlines() if line.strip()]
        ids = {q['id'] for q in questions if q.get('split') == 'test' and not q.get('is_trap')}
        rows = [json.loads(line) for line in matrix_path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError, KeyError):
        return None
    observed = {row['question_id']: row['hit'] for row in rows
                if row.get('engine') == engine and row.get('question_id') in ids
                and isinstance(row.get('hit'), bool) and row.get('n_results', 0) >= 0
                and not row.get('error')}
    if not observed:
        return None
    misses = sum(not hit for hit in observed.values())
    return {'engine': engine, 'misses': misses, 'n': len(observed), 'rate': misses / len(observed), 'split': 'test'}


def measured_results(path: Path) -> tuple[dict | None, str]:
    """No fixture, incomplete cohort, or simulated result becomes a pitch claim."""
    data = read_json(path)
    if not isinstance(data, dict):
        return None, 'Awaiting the held-out evaluation'
    if any(data.get(flag) for flag in ('fake', 'fixture', 'simulated')) or 'fake' in str(data.get('generated_from', '')).lower():
        return None, 'Awaiting measured evaluation results'
    try:
        Results.model_validate(data)
        n = data['n_test']
        if not isinstance(n, int) or n <= 0:
            raise ValueError('No test questions')
        for name in ('single', 'telem_default', 'second_opinion'):
            block = data['setups'][name]
            counts = [block[key] for key in ('correct', 'wrong', 'declined')]
            if any(not isinstance(value, int) or value < 0 for value in counts):
                raise ValueError('Invalid counts')
            if sum(counts) != n or block.get('n', n) != n:
                return None, 'Evaluation in progress · waiting for all three setups'
            for key in ('accuracy', 'wrong_rate', 'cost_per_q_total'):
                if not math.isfinite(block[key]) or block[key] < 0:
                    raise ValueError('Invalid measured value')
            if abs(block['accuracy'] - block['correct'] / n) > .00011 or abs(block['wrong_rate'] - block['wrong'] / n) > .00011:
                raise ValueError('Counts and rates disagree')
    except (ValueError, TypeError, KeyError):
        return None, 'Evaluation artifact is not ready to display'
    return data, f'{n} held-out questions per setup'


def install_presentation(app, settings, runner=None):
    # Import locally to avoid a cycle with app.create_app().
    from server.app import RaceRequest, stream_response, sse
    active_runs = 0

    @app.get('/presentation')
    async def presentation():
        results, status = measured_results(settings.results_path)
        return {'miss': first_search_misses(config.ROOT, settings.results_path.parent / 'matrix.jsonl', config.FIRST_ENGINE),
                'results': results, 'status': status,
                'first_engine': config.FIRST_ENGINE,
                'live_available': runner is not None or bool(os.environ.get('TELEM_API_KEY') and os.environ.get(f'{config.LLM_PROVIDER.upper()}_API_KEY'))}

    async def race_stream(body: RaceRequest, request: Request):
        nonlocal active_runs
        question_text = body.question.strip()
        if not question_text or len(question_text) > settings.max_question_chars:
            raise HTTPException(422, 'Enter a question within the length limit.')
        if active_runs + 2 > settings.max_active_runs:
            raise HTTPException(429, 'A race is already using the available capacity. Try again shortly.')
        active_runner = runner
        if active_runner is None:
            from so.agent import run_question
            active_runner = run_question
        question = Question(id=f'pitch-{uuid4().hex}', question=question_text, gold=[],
                            category='fact', source='race-ui', split='test')
        active_runs += 2

        async def events():
            nonlocal active_runs
            jobs = {}
            seen = {'left': 0, 'right': 0}
            ended = set()
            try:
                for side, setup in (('left', body.left), ('right', body.right)):
                    directory = settings.log_dir / question.id / side
                    directory.mkdir(parents=True, exist_ok=True)
                    async def execute(setup=setup, directory=directory):
                        async with asyncio.timeout(settings.run_timeout):
                            return await active_runner(question, setup, log_dir=str(directory))
                    jobs[side] = (directory, asyncio.create_task(execute()))
                yield sse('race', {'question': question_text, 'mode': 'live'})
                last_keepalive = asyncio.get_running_loop().time()
                while len(ended) < 2:
                    if await request.is_disconnected():
                        return
                    for side, (directory, task) in jobs.items():
                        if side in ended:
                            continue
                        for path in directory.glob('*.jsonl'):
                            raw = path.read_text()
                            # An incomplete append is read on the next poll.
                            for line in raw[:raw.rfind('\n') + 1].splitlines():
                                event = Event.model_validate_json(line)
                                if event.seq <= seen[side]:
                                    continue
                                seen[side] = event.seq
                                yield sse('log', {'side': side, 'event': event.model_dump()}, f'{side}-{event.seq}')
                                if event.type == 'run_end':
                                    ended.add(side)
                        if task.done() and side not in ended:
                            # Drain once more if the final append followed this read.
                            await asyncio.sleep(0)
                            latest = next(directory.glob('*.jsonl'), None)
                            if latest is not None:
                                rows = latest.read_text().splitlines()
                                if rows and json.loads(rows[-1]).get('seq', 0) > seen[side]:
                                    continue
                            yield sse('stream_error', {'side': side, 'message': 'This agent stopped before final totals. Its partial audit is still available.'})
                            ended.add(side)
                    if len(ended) == 2:
                        break
                    now = asyncio.get_running_loop().time()
                    if now - last_keepalive >= settings.keepalive_seconds:
                        yield ': keep-alive\n\n'
                        last_keepalive = now
                    await asyncio.sleep(settings.poll_seconds)
                yield sse('complete', {'question_id': question.id})
            finally:
                for _, task in jobs.values():
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*(task for _, task in jobs.values()), return_exceptions=True)
                active_runs -= 2
        return stream_response(events())

    # An explicit annotation is needed because RaceRequest is a local import.
    race_stream.__annotations__['body'] = RaceRequest
    app.post('/race-stream')(race_stream)
