"""A process result consumed while its notice waits must not start another model turn.

Exercise the real gateway FIFO, producer admission, SQLite cancellation and follower
execution. The model and registry consumption observation are the only stubbed seams.
"""
import asyncio
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.gateway import test_local_producer_admission as _producer_fixtures
from tests.waiting import async_wait_until

gateway = _producer_fixtures.gateway
messaging = _producer_fixtures.messaging


def _consumption_probe(monkeypatch, consumed):
    from tools.process_registry import process_registry
    monkeypatch.setattr(process_registry, 'is_completion_consumed', lambda process: process in consumed)
    monkeypatch.setattr(process_registry, 'get', lambda process: SimpleNamespace(id=process, started_at=1.0))


async def _hold_turn(gateway, authority, sid):
    viewer = await gateway.watch(authority, sid)
    gateway.gate = asyncio.Event()
    submitted = await viewer.dispatch({'id': 'hold', 'method': 'prompt.submit', 'params': {
        'session_id': sid, 'input_id': 'human-head', 'text': 'human head'}})
    assert 'result' in submitted, submitted
    await async_wait_until(lambda: gateway.turns, timeout=30, what='the human turn running')
    return viewer


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['completion', 'heartbeat'])
@pytest.mark.parametrize('profile', ['primary', 'secondary'])
async def test_consumed_notice_is_skipped_but_human_follower_runs(gateway, monkeypatch, kind, profile):
    consumed = set()
    _consumption_probe(monkeypatch, consumed)
    authority = getattr(gateway, profile)
    sid = (gateway.launch.session_id if profile == 'primary' else
           gateway.create(authority, gateway.connect(authority), 'secondary', 'cli').session_id)
    viewer = await _hold_turn(gateway, authority, sid)

    event = (gateway.tick(authority, sid, 1, process='proc_finished') if kind == 'heartbeat' else
             gateway.process_event(authority, sid, kind, process='proc_finished', exit_code=0))
    async with gateway.runner._completion_event_scope(event):
        assert await gateway.runner._inject_watch_notification('[proc_finished completed]', event) is True
    consumed.add('proc_finished')  # process wait/log returned the result in the head turn
    lookalike = '["completion","proc_finished",1.0]'
    follow = await viewer.dispatch({'id': 'follow', 'method': 'prompt.submit', 'params': {
        'session_id': sid, 'input_id': lookalike, 'text': '[INTERNAL NOTIFICATION] human question'}})
    assert 'result' in follow, follow
    gateway.gate.set()
    await gateway.settled(authority, sid, 3)

    assert [turn.text for turn in gateway.turns] == ['human head', '[INTERNAL NOTIFICATION] human question']
    rows = gateway.rows(authority, sid)
    assert [row['outcome'] for row in rows] == ['completed', 'cancelled', 'completed']


@pytest.mark.asyncio
async def test_consumed_messaging_completion_is_cancelled_before_claim(messaging, monkeypatch):
    from gateway.session_automation import _withdraw
    from gateway.session_contract import SessionRef

    authority = messaging.authority
    from hermes_state_runtime import list_session_admissions
    [first] = list_session_admissions(authority.db, session_id=messaging.owner)
    assert await _withdraw(authority, first)
    event = messaging.event('completion', exit_code=0, output='done')
    assert await messaging.runner._inject_watch_notification('[process completed]', event) is True
    _consumption_probe(monkeypatch, {'proc_job'})
    ref = SessionRef(authority.profile_id, messaging.owner)

    claimed, row = await authority._claim_next(ref, authority.sessions[messaging.owner])

    assert claimed is False, 'the consumed messaging result was claimed for inference'
    assert row['request_id'].startswith('["completion"')
    assert list_session_admissions(authority.db, session_id=messaging.owner) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('all_consumed', [False, True])
async def test_unread_failed_batch_member_is_never_discarded(gateway, monkeypatch, all_consumed):
    from gateway.session_automation import producer_identity

    authority, sid = gateway.primary, gateway.launch.session_id
    await _hold_turn(gateway, authority, sid)
    events = [gateway.process_event(authority, sid, 'completion', process=process, exit_code=code)
              for process, code in (('proc_done', 0), ('proc_failed', 1))]
    event = dict(events[0], _automation_identities=[producer_identity(gateway.runner, e) for e in events])
    assert await gateway.runner._inject_watch_notification('[proc_failed failed: unreported]', event) is True
    consumed = {'proc_done', 'proc_failed'} if all_consumed else {'proc_done'}
    _consumption_probe(monkeypatch, consumed)
    gateway.gate.set()
    await gateway.settled(authority, sid, 2)

    assert len(gateway.turns) == (1 if all_consumed else 2)
    assert gateway.rows(authority, sid)[-1]['outcome'] == ('cancelled' if all_consumed else 'completed')
    if not all_consumed:
        assert 'proc_failed failed: unreported' in gateway.turns[-1].text


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
@pytest.mark.parametrize('same_incarnation', [True, False])
async def test_real_process_wait_consumption_respects_spawn_identity(gateway, same_incarnation):
    from tools.process_registry import process_registry

    authority, sid = gateway.primary, gateway.launch.session_id
    await _hold_turn(gateway, authority, sid)
    command = [sys.executable, '-c', 'print("real process result")']
    child = await asyncio.to_thread(subprocess.Popen, command, cwd=gateway.tmp_path,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
    process = process_registry.adopt_local(child, command=shlex.join(command), cwd=str(gateway.tmp_path),
                                           session_key=authority.sessions[sid].route, notify_on_complete=False)
    assert await asyncio.to_thread(process._completion_event.wait, 20), 'child did not finish'
    assert not process_registry.is_completion_consumed(process.id)
    event = gateway.process_event(authority, sid, 'completion', process=process.id, exit_code=0,
                                  started_at=process.started_at if same_incarnation else process.started_at + 1)
    assert await gateway.runner._inject_watch_notification('[real process result]', event) is True
    result = await asyncio.to_thread(process_registry.wait, process.id, 20)
    assert result['exit_code'] == 0 and 'real process result' in result['output']
    assert process_registry.is_completion_consumed(process.id)
    gateway.gate.set()
    await gateway.settled(authority, sid, 2)

    assert len(gateway.turns) == (1 if same_incarnation else 2)
    assert gateway.rows(authority, sid)[-1]['outcome'] == ('cancelled' if same_incarnation else 'completed')


def test_internal_notification_supplies_a_recognized_explicit_silence_marker():
    from gateway.response_filters import LIVE_GATEWAY_SILENT_MARKERS, is_intentional_silence_response
    from gateway.run_notifications import _mark_internal_notification

    text = '[IMPORTANT: process completed normally (exit code 0)]'
    marked = _mark_internal_notification(text)
    assert marked.startswith(text)
    assert any(marker in marked and is_intentional_silence_response(marker)
               for marker in LIVE_GATEWAY_SILENT_MARKERS)
    assert _mark_internal_notification(marked) == marked
