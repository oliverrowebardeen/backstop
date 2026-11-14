import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.o4.access import KEY, authenticated_client

from server.app import ServerSettings, create_app
from server.presentation import first_search_misses, measured_results
from so import config
from tests.o4.fakes import run_question, interrupted_question


def settings(tmp_path):
    return replace(ServerSettings.from_config(), log_dir=tmp_path / 'logs', demo_dir=tmp_path / 'demo',
                   fixture_dir=None, results_path=tmp_path / 'results.json', frontier_path=tmp_path / 'frontier.json',
                   run_timeout=2, poll_seconds=.005, keepalive_seconds=.1)


def test_first_search_misses_uses_test_only_and_observed_denominator(tmp_path):
    (tmp_path/'data').mkdir()
    questions=[{'id':'test-1','split':'test'},{'id':'test-2','split':'test'}, {'id':'learn-1','split':'learn'}, {'id':'trap-1','split':'test','is_trap':True}]
    (tmp_path/'data/questions.jsonl').write_text('\n'.join(map(json.dumps,questions)))
    rows=[{'question_id':q['id'],'engine':'tavily','hit':False,'n_results':5} for q in questions]
    rows += [{'question_id':'test-2','engine':'tavily','hit':True,'n_results':5}, {'question_id':'test-2','engine':'exa','hit':False}]
    matrix=tmp_path/'matrix.jsonl';matrix.write_text('\n'.join(map(json.dumps,rows)))
    result=first_search_misses(tmp_path,matrix,'tavily')
    assert result['n']==2 and result['misses']==1 and result['rate']==.5


def measured():
    block={'n':10,'correct':7,'wrong':2,'declined':1,'accuracy':.7,'wrong_rate':.2,'cost_per_q_total':.02}
    return {'generated_at':'2026-11-14T20:00:00Z','n_test':10,'n_traps':0,'setups':{key:dict(block) for key in ['single','telem_default','second_opinion']}}


def test_unfinished_and_fake_results_never_become_claims(tmp_path):
    path=tmp_path/'results.json'
    assert measured_results(path)[0] is None
    data=measured();data['setups']['second_opinion']['n']=8;path.write_text(json.dumps(data))
    assert measured_results(path)[0] is None
    data=measured();data['fixture']=True;path.write_text(json.dumps(data))
    assert measured_results(path)[0] is None
    data=measured();data['setups']['single']['accuracy']=.8;path.write_text(json.dumps(data))
    assert measured_results(path)[0] is None
    data=measured();path.write_text(json.dumps(data));assert measured_results(path)[0]==data


def sse_rows(response):
    result=[]
    for block in response.text.split('\n\n'):
        fields=dict(line.split(': ',1) for line in block.splitlines() if ': ' in line)
        if 'data' in fields: result.append((fields.get('event'),json.loads(fields['data'])))
    return result


@pytest.fixture
def live_client(monkeypatch):
    monkeypatch.setenv("BACKSTOP_API_KEYS", KEY)

    def build(configured, runner):
        return authenticated_client(create_app(settings=replace(configured, live_races=True), runner=runner))

    return build


def test_streamed_race_contains_both_complete_exact_event_logs(tmp_path, live_client):
    with live_client(settings(tmp_path), run_question) as client:
        response=client.post('/race-stream',json={'question':'A test question','left':'single','right':'second_opinion'})
        assert response.status_code==200
        rows=sse_rows(response)
        assert rows[0][0]=='race' and rows[-1][0]=='complete'
        for side in ['left','right']:
            events=[data['event'] for kind,data in rows if kind=='log' and data['side']==side]
            assert events[0]['type']=='run_start' and events[-1]['type']=='run_end'
            stored=next((tmp_path/'logs').glob(f'*/{side}/*.jsonl'))
            assert events==[json.loads(line) for line in stored.read_text().splitlines()]
        assert client.get('/presentation').json()['live_available'] is True


def test_stream_failure_is_explicit_and_capacity_is_released(tmp_path, live_client):
    with live_client(settings(tmp_path), interrupted_question) as client:
        for _ in range(2):
            response=client.post('/race-stream',json={'question':'A test question'})
            rows=sse_rows(response)
            errors=[data for kind,data in rows if kind=='stream_error']
            assert {error['side'] for error in errors}=={'left','right'}
            assert rows[-1][0]=='complete'


def test_stream_rejects_blank_and_oversize_questions(tmp_path, live_client):
    with live_client(settings(tmp_path), run_question) as client:
        assert client.post('/race-stream',json={'question':'  '}).status_code==422
        assert client.post('/race-stream',json={'question':'x'*2001}).status_code==422


def test_provider_limit_after_cleanup_totals_is_a_stream_failure(tmp_path, live_client):
    from tests.o4.fakes import usage_limit_question
    with live_client(settings(tmp_path), usage_limit_question) as client:
        response=client.post('/race-stream',json={'question':'A test question'})
        rows=sse_rows(response)
        for side in ['left','right']:
            ends=[data for kind,data in rows if kind=='log' and data['side']==side and data['event']['type']=='run_end']
            failures=[data for kind,data in rows if kind=='stream_error' and data['side']==side]
            assert len(ends)==1 and len(failures)==1
            assert 'usage limit' in failures[0]['message'].lower()
        assert rows[-1][0]=='complete'
