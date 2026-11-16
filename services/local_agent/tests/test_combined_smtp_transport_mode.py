"""Saved TLS mode reaches the owned worker; all data are synthetic."""
from contextlib import nullcontext
from types import SimpleNamespace
import json
import struct
import threading
import pytest
from home_study_agent import daily_report_combined_owned_delivery as adapter, daily_report_combined_delivery as combined, smtp_owned_process
from home_study_agent.notification_channels import SmtpChannel
from test_combined_report_real_sources_fixture import synthetic_combined_family
from test_daily_report_combined_snapshot import candidate
from test_daily_report_combined_delivery import configured
from test_combined_report_loopback_candidate import sink


@pytest.mark.parametrize('config,expected',[
    ({'use_ssl':True,'starttls':False,'port':465},(True,False,465)),
    ({'use_ssl':True},(True,False,465)),
    ({'use_ssl':False,'starttls':True,'port':'587'},(False,True,587)),
    ({},(False,True,587)),
    ({'use_ssl':False,'starttls':False,'port':2525},(False,False,2525)),
])
def test_saved_mode_mapping(config,expected):
    assert adapter._transport(config)==expected


@pytest.mark.parametrize('config',[
    {'use_ssl':True,'starttls':True}, {'use_ssl':'true'}, {'starttls':'false'},
    {'use_ssl':False,'port':25.5}, {'port':True}, {'port':0}, {'port':65536},
])
def test_invalid_mode_or_port_cannot_be_silently_changed(config):
    with pytest.raises(ValueError):adapter._transport(config)


@pytest.mark.parametrize('use_ssl,starttls',[(True,False),(False,True),(False,False)])
def test_owner_serializes_exact_mode_only_in_private_frame(monkeypatch,use_ssl,starttls):
    captured=[]
    def inspect_frame(self,frame,deadline):
        n=struct.unpack('!I',frame[:4])[0];payload=json.loads(frame[4:])
        assert n==len(frame)-4
        captured.append((payload['ssl'],payload['starttls'],payload['username'],payload['password']))
        self.confirmed=self.cleaned=True;self.outcome='not_submitted'
    monkeypatch.setattr(smtp_owned_process.OwnedSmtpProcess,'_run',inspect_frame)
    process=smtp_owned_process.OwnedSmtpProcess(nullcontext,lambda:True,lambda *args:None)
    username,password=('synthetic-user','synthetic-secret') if use_ssl or starttls else ('','')
    result=process.submit(host='127.0.0.1',port=2525,username=username,password=password,
        sender='sender@example.test',recipient='parent@example.test',mime_bytes=b'Subject: x\r\n\r\nx',
        timeout_seconds=2,use_ssl=use_ssl,starttls=starttls)
    assert result['status']=='not_submitted'
    assert captured==[(use_ssl,starttls,username,password)]


@pytest.mark.parametrize('use_ssl,starttls,user,password,code',[
    (True,True,'','','SMTP_TLS_MODE_INVALID'),
    ('true',False,'','','SMTP_TLS_MODE_INVALID'),
    (False,False,'synthetic-user','synthetic-secret','SMTP_PLAINTEXT_AUTH_FORBIDDEN'),
    (True,False,'synthetic-user','','SMTP_AUTH_CONFIGURATION_INCOMPLETE'),
    (False,True,'','synthetic-secret','SMTP_AUTH_CONFIGURATION_INCOMPLETE'),
])
def test_invalid_or_plaintext_credentials_are_rejected_before_spawn(monkeypatch,use_ssl,starttls,user,password,code):
    calls=[]
    monkeypatch.setattr(smtp_owned_process.subprocess,'Popen',lambda *a,**k:calls.append(True))
    process=smtp_owned_process.OwnedSmtpProcess(nullcontext,lambda:True,lambda *args:None)
    with pytest.raises(ValueError,match=code):
        process.submit(host='127.0.0.1',port=2525,username=user,password=password,
            sender='sender@example.test',recipient='parent@example.test',mime_bytes=b'x',
            timeout_seconds=2,use_ssl=use_ssl,starttls=starttls)
    assert calls==[] and not process.started


def test_public_owner_authority_is_not_a_request_or_environment_switch(monkeypatch):
    process=smtp_owned_process.OwnedSmtpProcess(nullcontext,lambda:True,lambda *args:None)
    fields=dict(host='smtp.synthetic.test',port=465,username='synthetic-user',password='synthetic-secret',
        sender='sender@example.test',recipient='parent@example.test',mime_bytes=b'x',
        timeout_seconds=2,use_ssl=True,starttls=False)
    monkeypatch.setenv('HSC_ALLOW_PUBLIC_SMTP','1')
    with pytest.raises(ValueError,match='SMTP_PUBLIC_AUTHORITY_INVALID'):
        process.submit(**fields,_public_authority=True)
    with pytest.raises(ValueError,match='COMBINED_SOURCE_WITHDRAWAL_GUARD_UNAVAILABLE'):
        process.submit(**fields,_public_authority=smtp_owned_process._COMBINED_PUBLIC_TRANSPORT_AUTHORITY)
    assert not process.started


@pytest.mark.parametrize('host,use_ssl,starttls,error',[
    ('192.0.2.1',True,False,'SMTP_PUBLIC_HOST_INVALID'),
    ('localhost',True,False,'SMTP_PUBLIC_HOST_INVALID'),
    ('smtp.synthetic.test',False,False,'SMTP_PUBLIC_TLS_REQUIRED'),
])
def test_public_owner_rejects_literal_or_plaintext_before_spawn(monkeypatch,host,use_ssl,starttls,error):
    monkeypatch.setattr(combined,'_source_withdrawal_guard_proven',lambda:True)
    process=smtp_owned_process.OwnedSmtpProcess(nullcontext,lambda:True,lambda *args:None)
    with pytest.raises(ValueError,match=error):
        process.submit(host=host,port=465,username='',password='',sender='sender@example.test',
            recipient='parent@example.test',mime_bytes=b'x',timeout_seconds=2,use_ssl=use_ssl,starttls=starttls,
            _public_authority=smtp_owned_process._COMBINED_PUBLIC_TRANSPORT_AUTHORITY)
    assert not process.started


@pytest.mark.parametrize('security',[
    {'use_ssl':True,'starttls':False,'port':465},
    {'use_ssl':True,'port':465},
    {'use_ssl':False,'starttls':True,'port':587},
])
def test_adapter_uses_actual_saved_security_config(candidate,monkeypatch,security):
    f,batch,snapshot,_=candidate;captured=[]
    monkeypatch.setattr(combined,'_source_withdrawal_guard_proven',lambda:True)
    with sink() as smtp:
        service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
        service.save_channel_config('email',{'host':'127.0.0.1','username':'synthetic-user',
            'password':'synthetic-secret','from_address':'sender@example.test','allow_external_delivery':True,**security})
        class Verification:
            def send(self,*args):return SimpleNamespace(provider_message_id=None)
        service.adapters['email']=Verification()
        assert service.test_channel(f['child'],'email','transport-test-'+str(security),recipient_id=f['recipient'])['status']=='passed'
        service.adapters['email']=SmtpChannel()
        class FakeOwner:
            def __init__(self,*args):pass
            def submit(self,**kwargs):captured.append(kwargs);return {'status':'not_submitted'}
        monkeypatch.setattr(adapter,'OwnedSmtpProcess',FakeOwner)
        assert adapter.submit(service,f['telemetry'],snapshot)['status']=='blocked'
        assert len(captured)==1 and not smtp.messages
        assert (captured[0]['use_ssl'],captured[0]['starttls'],captured[0]['port'])==adapter._transport(security)
        assert (captured[0]['username'],captured[0]['password'])==('synthetic-user','synthetic-secret')


from test_smtp_owned_worker_tls import certificates,smtp_factory


@pytest.mark.parametrize('mode', ['implicit', 'starttls'])
@pytest.mark.parametrize('revoke_after_ready', [False, True])
def test_public_adapter_keeps_double_preflight_and_atomic_claim_on_isolated_tls(
        candidate,monkeypatch,certificates,smtp_factory,revoke_after_ready,mode):
    import sys
    from test_smtp_owned_worker_tls import WORKER
    from test_daily_report_combined_owned_delivery import _test_owner
    f,batch,snapshot,_=candidate;_test_owner(monkeypatch)
    ca,make_context=certificates
    server=smtp_factory(make_context(False),implicit=mode=='implicit')
    service=configured(f,snapshot,server.server_address[1],monkeypatch)
    service.save_channel_config('email',{'host':'smtp.synthetic.test','port':server.server_address[1],
        'username':'synthetic-user','password':'synthetic-secret','from_address':'sender@example.test',
        'allow_external_delivery':True,'use_ssl':mode=='implicit','starttls':mode=='starttls'})
    class Verification:
        def send(self,*args): return SimpleNamespace(provider_message_id=None)
    service.adapters['email']=Verification()
    assert service.test_channel(f['child'],'email','public-transport-synthetic',recipient_id=f['recipient'])['status']=='passed'
    service.adapters['email']=SmtpChannel()
    harness=("import ssl,socket,runpy,sys; real=socket.getaddrinfo; "
        "socket.getaddrinfo=lambda h,p,*a,**k: real('93.184.216.34' if h=='smtp.synthetic.test' else h,p,*a,**k); "
        "old=socket.socket.connect; "
        "exec('def connect(s,a):\\n if a[0]!=\"93.184.216.34\": raise RuntimeError(\"external blocked\")\\n return old(s,(\"127.0.0.1\",a[1]))'); "
        "socket.socket.connect=connect; original=ssl.create_default_context; "
        "exec('def context():\\n c=original()\\n c.load_verify_locations(cafile=sys.argv[2])\\n return c'); "
        "ssl.create_default_context=context; m=runpy.run_path(sys.argv[1],run_name='isolated_worker_test'); "
        "m['run'].__globals__['_public_transport_proven']=lambda:True; sys.exit(m['run'](sys.stdin.buffer,sys.stdout.buffer))")
    executable=getattr(sys,'_base_executable',sys.executable) if sys.platform=='win32' else sys.executable
    monkeypatch.setattr(smtp_owned_process.OwnedSmtpProcess,'_worker_command',
        lambda self:[executable,'-I','-S','-B','-c',harness,str(WORKER),str(ca)])
    original=adapter._view; calls=[]
    def view(*args,**kwargs):
        assert f['db']._external_effect_guard_owned()
        calls.append(True)
        if revoke_after_ready and len(calls)==3: raise ValueError('RECIPIENT_NOT_AUTHORIZED')
        return original(*args,**kwargs)
    monkeypatch.setattr(adapter,'_view',view)
    result=adapter.submit(service,f['telemetry'],snapshot,timeout_seconds=12)
    assert len(calls)>=3 and server.sni==['smtp.synthetic.test']
    if revoke_after_ready:
        assert result=={'status':'blocked','reason':'RECIPIENT_NOT_AUTHORIZED'}
        assert not server.data
        with f['db'].connect() as c:
            assert c.execute('SELECT COUNT(*) FROM combined_daily_delivery').fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM smtp_owned_worker_journal').fetchone()[0]==0
    else:
        assert result=={'status':'sent'} and len(server.data)==1
        with f['db'].connect() as c:
            assert [c.execute('SELECT status FROM '+table).fetchone()[0] for table in
                    ('combined_daily_delivery','daily_observation_delivery_ledger','combined_diagnostic_member')]==['sent']*3
            assert tuple(c.execute('SELECT status,result FROM smtp_owned_worker_journal').fetchone())==('stopped','accepted')


@pytest.mark.parametrize('mode',['starttls_not_offered','implicit_ssl_to_plaintext'])
def test_real_owned_worker_never_downgrades_or_authenticates_plaintext(certificates,smtp_factory,mode):
    _ca,make_context=certificates
    server=smtp_factory(make_context(True),advertise_starttls=False)
    persisted=[]
    process=smtp_owned_process.OwnedSmtpProcess(nullcontext,lambda:True,lambda *args:persisted.append(True))
    result=process.submit(host='127.0.0.1',port=server.server_address[1],username='synthetic-user',
        password='synthetic-secret',sender='sender@example.test',recipient='parent@example.test',
        mime_bytes=b'Subject: synthetic\r\n\r\nx',timeout_seconds=3,
        use_ssl=(mode=='implicit_ssl_to_plaintext'),starttls=(mode=='starttls_not_offered'))
    assert result['status']=='not_submitted' and result['exit_confirmed']
    assert not persisted and not server.auth and not server.data and b'DATA' not in server.commands
