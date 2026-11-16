from home_study_agent.voice_playback import VoicePlaybackGuard


def test_clear_known_child_preserves_other_child_and_refuses_old_playback_registration():
    guard=VoicePlaybackGuard()
    text='Synthetic original passage for exact ephemeral echo recognition.'
    guard.record('session-one','playback',text,'started',child_id='one')
    guard.record('session-two','playback',text,'started',child_id='two')
    assert guard.clear_child('one')==1
    assert not guard.matches('session-one','playback',text)
    assert guard.matches('session-two','playback',text)
    assert not guard.record('session-one','playback',text,'started',child_id='one')['registered']
    assert not guard.record('session-one','playback',text,'ended',child_id='one')['registered']
    assert guard.record('session-one','new-playback','New authorized speech.','started',child_id='one')['registered']
    assert text not in repr(guard.revoked_playbacks)
    assert guard.clear_child('missing')==0


def test_whole_database_restore_clears_all_old_ephemeral_text_not_only_current_child():
    guard=VoicePlaybackGuard()
    for child in ('one','two'):
        guard.record(child,child,'Synthetic pre-restore speech.','started',child_id=child)
    assert guard.clear_all()==2
    assert not guard.entries
    assert not guard.record('two','two','Synthetic pre-restore speech.','started',child_id='two')['registered']
