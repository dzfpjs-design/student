"""Synthetic native input uses the actual reserved-purpose ingress and witness.

The original18-case proposal fixture remains legacy and is never rewritten.
No source is upgraded by editing SQLite; the trusted-authorizer stub applies
only to this isolated public synthetic ingress fixture, not production HTTP.
"""
import time
import pytest
from home_study_agent.realtime_native_input import record_native_input
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService


@pytest.fixture
def scenario(legacy_scenario):
 values=list(legacy_scenario)
 client,db,gateway,root,old,headers,service,original,seen,authorize=values
 sid=root.split('/')[-1]
 captures=VoiceCaptureBindingService(db,page_source_store=service.page_source_store)
 def proposal(words,args,**kwargs):
  if kwargs.get('voice') is None:
   key=kwargs.get('key','new')
   voice=record_native_input(captures,sid,{'version':1,'local_input_id':'public-native-'+key,
    'transcript':words,'captured_at_ms':int(time.time()*1000),
    'capture_reference':{'child_id':'child-primary-4','question_reference':kwargs.get('reference')}},authorize=lambda *a:True)
   assert voice['purpose']=='native_cloud_dialogue' and voice['source_kind']=='realtime_transcript'
   kwargs['voice']=voice
  return original(words,args,**kwargs)
 values[7]=proposal
 return tuple(values)
