"""Record one genuine pair for the pitch. Uses the existing paid-call guards."""
import asyncio
import json
from pathlib import Path

from so.agent import run_question
from so.contracts import Question
from so import config


async def main():
    root = config.ROOT / 'demo'
    root.mkdir(exist_ok=True)
    question = Question(id='golden-stripe-site', question="What was Stripe's valuation in its 2023 round?",
                        gold=[], category='finance', source='presentation', split='test')
    names = {'left': 'single', 'right': 'second_opinion'}
    async def record(side, setup):
        folder = root / 'golden-stripe' / side
        folder.mkdir(parents=True, exist_ok=True)
        answer = await run_question(question, setup, log_dir=str(folder))
        logs = sorted(folder.glob('*.jsonl'), key=lambda p: p.stat().st_mtime)
        path = logs[-1]
        events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if events[-1]['type'] != 'run_end':
            raise RuntimeError('Golden run did not finish')
        print(setup, answer.status, answer.final, events[-1]['data']['cost_usd_total'], flush=True)
        return side, str(path.relative_to(root))
    async with asyncio.timeout(240):
        pairs = dict(await asyncio.gather(*(record(side, setup) for side, setup in names.items())))
    manifest = {'title': 'Stripe’s 2023 valuation', 'fixture': False, 'golden': True, **pairs}
    (root / 'golden-stripe.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Recorded demo/golden-stripe.json', flush=True)

if __name__ == '__main__':
    asyncio.run(main())
