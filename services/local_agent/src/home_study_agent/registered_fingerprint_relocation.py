"""Exact target fingerprint relocation; source evidence is never rewritten.

Internal preparation only. This module confers no source, destination, ownership
or publication capability. Callers supply their original held/owned connections.
"""
import hashlib,json,re,sqlite3
from .registered_asset_snapshot import (canonical,digest,need,parse_registered_fingerprint,
    registered_fingerprint_witness)

CODE='REGISTERED_RESTORE_FINGERPRINT_RELOCATION_REQUIRED'
FORMAT='hsc_registered_fingerprint_relocation_v1'
NAME='.hsc-fingerprint-relocation-v1.json'
ROLE='fingerprint_provenance'
MAX_CHANGES=100000
MAX_METADATA_BYTES=64*1024*1024


def planned_fingerprint(role,source,asset,precision_ns):
    """Authenticate the exact source witness before deriving any target value."""
    need(type(precision_ns)is int and precision_ns in {1,100},CODE)
    need(type(asset)is dict and asset.get('role')==role and role in {'pdf','image','video'},CODE)
    size,mtime=parse_registered_fingerprint(source)
    need(0<=mtime<2**63,CODE)
    from .registered_asset_restore import need as restore_need
    restore_need(size==asset.get('bytes'),'REGISTERED_RESTORE_FINGERPRINT_WITNESS_REQUIRED')
    witness=digest(canonical(registered_fingerprint_witness(role,source,asset.get('sha256'))))
    need(type(asset.get('version_sha256s'))is list,CODE)
    restore_need(witness in asset['version_sha256s'],'REGISTERED_RESTORE_FINGERPRINT_WITNESS_REQUIRED')
    target=mtime-mtime%precision_ns
    return {'source_fingerprint':source,'target_fingerprint':f'size:{size}:mtime_ns:{target}',
            'source_sha256':asset['sha256'],'bytes':size,'source_mtime_ns':mtime,
            'target_mtime_ns':target,'precision_ns':precision_ns,'role':role}


def merge_target(targets,relative,value):
    """A physical target cannot have conflicting authenticated source claims."""
    from .registered_asset_snapshot import Policy,safe_relative
    safe_relative(relative,Policy(MAX_CHANGES,64,1024,MAX_METADATA_BYTES,2**53-1,2**53-1))
    keys=('source_fingerprint','target_fingerprint','source_sha256','bytes','precision_ns')
    need(relative not in targets or all(targets[relative][key]==value[key]for key in keys),CODE)
    targets[relative]=value


def metadata_changes(values):
    need(type(values)is list and bool(values),CODE)
    result={}
    for value in values:
        need(type(value)is tuple and len(value)==3,CODE)
        key,source,target=value
        need(type(key)is str and 0<len(key.encode('utf-8'))<=1024,CODE)
        source_size,source_ns=parse_registered_fingerprint(source);target_size,target_ns=parse_registered_fingerprint(target)
        need(0<=source_ns<2**63 and source_size==target_size and source!=target
             and target_ns==source_ns-source_ns%100,CODE)
        need(key not in result or result[key]==(source,target),CODE)
        result[key]=(source,target)
    return result


def _identifier(value):
    need(type(value)is str and re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',value)is not None,CODE)
    return '"'+value+'"'


def metadata_projection(connection,changes=()):
    """Hash the entire schema and every cell, substituting only exact book CAS."""
    updates=metadata_changes(changes)if changes else {}
    sha=hashlib.sha256();seen=set()
    schema=list(connection.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))
    for row in schema:
        sha.update(canonical(list(row)));sha.update(b'\n')
    for kind,name,table,sql in schema:
        if kind!='table':continue
        need(type(sql)is str and not sql.lstrip().upper().startswith('CREATE VIRTUAL'),CODE)
        quoted=_identifier(name)
        columns=[row[1]for row in connection.execute('PRAGMA table_info('+quoted+')')]
        need(columns and '_rowid_'not in columns,CODE)
        for column in columns:_identifier(column)
        sha.update(canonical([name,columns]));sha.update(b'\n')
        count=0
        info=list(connection.execute('PRAGMA table_info('+quoted+')'))
        primary=[r[1]for r in sorted(info,key=lambda r:r[5])if r[5]]
        if name=='textbook_book':
            need(primary==['book_id']and 'fingerprint'in columns,CODE)
        # Book CAS never changes its unique identity. Other tables have no
        # substituted cells, so PK order (or the original rowid) is stable.
        order=','.join(_identifier(column)for column in primary)if primary else '_rowid_'
        for row in connection.execute('SELECT * FROM '+quoted+' ORDER BY '+order):
            count+=1
            values=dict(zip(columns,row))
            if name=='textbook_book'and values.get('book_id')in updates:
                key=values['book_id'];source,target=updates[key]
                need(values.get('fingerprint')==source,CODE)
                values['fingerprint']=target;seen.add(key)
            encoded=[]
            for column in columns:
                value=values[column]
                if isinstance(value,bytes):value=['blob_sha256',digest(value),len(value)]
                elif isinstance(value,float):value=['float_hex',value.hex()]
                encoded.append(value)
            sha.update(canonical(encoded));sha.update(b'\n')
        sha.update(canonical(['count',count]))
    need(seen==set(updates),CODE)
    return sha.hexdigest()


def rewrite_metadata_connection(source,target,changes):
    """Clone source then CAS only book fingerprints into an original owned DB.

    The original caller owns and closes both exact connections and validates
    source SHA plus destination native identity. The restrictive authorizer stays
    until the write connection closes; verification uses a fresh read-only one.
    """
    updates=metadata_changes(changes)
    need(source.execute('PRAGMA query_only').fetchone()[0]==1,CODE)
    need(target.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]==0,CODE)
    need(source.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='textbook_book'").fetchone()[0]==0,CODE)
    expected=metadata_projection(source,changes)
    source.backup(target)
    need(target.execute('PRAGMA journal_mode=DELETE').fetchone()[0]=='delete',CODE)
    target.execute('PRAGMA trusted_schema=OFF');target.execute('PRAGMA synchronous=FULL')
    denied=[False]
    def authorize(action,first,second,database,origin):
        ok=origin is None and ((action==sqlite3.SQLITE_UPDATE and database=='main'and first=='textbook_book'and second=='fingerprint')
          or(action==sqlite3.SQLITE_READ and database=='main'and first=='textbook_book'and second in {'book_id','fingerprint'})
          or(action==sqlite3.SQLITE_TRANSACTION and first in {'BEGIN','COMMIT','ROLLBACK'}))
        if not ok:denied[0]=True
        return sqlite3.SQLITE_OK if ok else sqlite3.SQLITE_DENY
    target.set_authorizer(authorize)
    try:
        target.execute('BEGIN IMMEDIATE')
        for key,(before,after)in sorted(updates.items()):
            result=target.execute('UPDATE textbook_book SET fingerprint=? WHERE book_id=? AND fingerprint=?',(after,key,before))
            need(result.rowcount==1,CODE)
        target.commit()
    except BaseException:
        target.rollback()
        if denied[0]:need(False,CODE)
        raise
    return expected


def verify_metadata_connection(connection,expected):
    need(type(expected)is str and re.fullmatch('[a-f0-9]{64}',expected)is not None,CODE)
    need(connection.execute('PRAGMA query_only').fetchone()[0]==1,CODE)
    need(metadata_projection(connection)==expected,CODE)
    need(connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok',CODE)
    need(connection.execute('PRAGMA foreign_key_check').fetchone()is None,CODE)


def collect_plan(connection,manifest,policy,precision_ns,metadata_source):
    """Source-only planning shared by both original preparation entry points.

    metadata_source is the caller's original authenticated metadata reader. Its
    context stays live while every book fingerprint witness is checked. Nothing
    returned here is an ownership or publication capability.
    """
    from .registered_asset_snapshot import record_key
    from .registered_asset_restore import _Plan,_columns,_source_path,registered_root,need as restore_need
    need(type(precision_ns)is int and precision_ns in {1,100},CODE)
    original=_Plan(None,None,manifest,policy,None)
    assets=original.assets;targets={};references=[];main=[];metadata={};groups={}
    def logical(key,role,relative):
        name='registered/kg/'+record_key('kg',key)+'/'+role+'/'+relative
        need(name in assets,CODE);return name
    def fingerprint(key,role,root,relative,source):
        need(type(root)is str and type(relative)is str,CODE)
        name=logical(key,role,relative);item=assets[name]
        need(item['kind']=='kg'and item['record_sha256']==record_key('kg',key)
             and item['relative_path']==relative,CODE)
        value=planned_fingerprint(role,source,item,precision_ns)
        target=registered_root('kg',root,policy)+'/'+relative
        merge_target(targets,target,value)
        references.append({'logical_name':name,'target_relative':target,'corpus_id':key,**value})
        return name,value
    if not _columns(connection,'kg_corpus'):
        return {'precision_ns':precision_ns,'targets':targets,'references':references,'main_changes':main,'metadata_changes':metadata,'corpus_groups':groups}
    extra=_columns(connection,'kg_corpus')
    pdf='pdf_root'if 'pdf_root'in extra else'NULL AS pdf_root'
    image='image_root'if 'image_root'in extra else'NULL AS image_root'
    for corpus in original.rows(connection.execute('SELECT corpus_id,transcript_root,video_root,metadata_db_path,'+pdf+','+image+' FROM kg_corpus ORDER BY corpus_id')):
        key=corpus['corpus_id']
        if corpus['transcript_root']in {'bundle:'+key,'local://family-textbook-ocr-pages'}:continue
        groups[key]=registered_root('kg',corpus['transcript_root'],policy)
        if 'video_fingerprint'in _columns(connection,'kg_resource'):
            for row in original.rows(connection.execute('SELECT resource_id,video_relative_path,video_fingerprint FROM kg_resource WHERE corpus_id=? ORDER BY resource_id',(key,))):
                if not row['video_relative_path']or row['video_fingerprint']is None:continue
                name,value=fingerprint(key,'video',corpus['video_root'],row['video_relative_path'],row['video_fingerprint'])
                if value['target_fingerprint']!=value['source_fingerprint']:
                    main.append({'table':'kg_resource','id_column':'resource_id','id':row['resource_id'],
                      'column':'video_fingerprint','corpus_id':key,'resource_id':row['resource_id'],
                      'source_fingerprint':value['source_fingerprint'],'target_fingerprint':value['target_fingerprint'],'logical_name':name})
        if 'image_fingerprint'in _columns(connection,'kg_document_page'):
            for row in original.rows(connection.execute('SELECT p.page_id,p.resource_id,p.image_relative_path,p.image_fingerprint FROM kg_document_page p JOIN kg_resource r ON r.resource_id=p.resource_id WHERE r.corpus_id=? ORDER BY p.page_id',(key,))):
                if not row['image_relative_path']or row['image_fingerprint']is None:continue
                name,value=fingerprint(key,'image',corpus['image_root'],row['image_relative_path'],row['image_fingerprint'])
                if value['target_fingerprint']!=value['source_fingerprint']:
                    main.append({'table':'kg_document_page','id_column':'page_id','id':row['page_id'],
                      'column':'image_fingerprint','corpus_id':key,'resource_id':row['resource_id'],
                      'source_fingerprint':value['source_fingerprint'],'target_fingerprint':value['target_fingerprint'],'logical_name':name})
        if corpus['metadata_db_path']:
            source=_source_path(corpus['metadata_db_path'],policy)
            name=logical(key,'metadata',source.name);item=assets[name]
            need(item['kind']=='kg'and item['role']=='metadata'and item['storage']=='sqlite_backup'
                and item['record_sha256']==record_key('kg',key)and item['relative_path']==source.name,CODE)
            updates=[]
            with metadata_source(name,item)as reader:
                columns=_columns(reader,'textbook_book')
                restore_need(not columns or 'fingerprint'in columns,'REGISTERED_RESTORE_FINGERPRINT_WITNESS_REQUIRED')
                need(not columns or {'book_id','relative_pdf_path'}<=columns,CODE)
                if columns:
                    for row in original.rows(reader.execute('SELECT book_id,relative_pdf_path,fingerprint FROM textbook_book ORDER BY book_id')):
                        _,value=fingerprint(key,'pdf',corpus['pdf_root'],row['relative_pdf_path'],row['fingerprint'])
                        if value['target_fingerprint']!=value['source_fingerprint']:
                            updates.append((row['book_id'],value['source_fingerprint'],value['target_fingerprint']))
            if updates:
                target=registered_root('kg',str(source.parent),policy)+'/'+source.name
                need(name not in metadata,CODE)
                metadata[name]={'source_sha256':item['sha256'],'source_bytes':item['bytes'],
                    'target_relative':target,'changes':updates,'corpus_id':key}
    return {'precision_ns':precision_ns,'targets':targets,'references':references,'main_changes':main,'metadata_changes':metadata,'corpus_groups':groups}


def _closed_pairs(items):
    value={}
    for key,item in items:
        need(key not in value,CODE);value[key]=item
    return value


def _hex(value):
    need(type(value)is str and re.fullmatch('[a-f0-9]{64}',value)is not None,CODE)


def _logical(value,role=None):
    from .registered_asset_snapshot import Policy,safe_relative
    parts=safe_relative(value,Policy(MAX_CHANGES,64,1024,MAX_METADATA_BYTES,2**53-1,2**53-1))
    need(len(parts)>=5 and parts[:2]==['registered','kg'],CODE);_hex(parts[2])
    if role is not None:need(parts[3]==role,CODE)


def decode_provenance(raw,maximum=MAX_METADATA_BYTES):
    """Historical derivations only: this JSON never supplies path/SQL authority."""
    need(type(raw)is bytes and type(maximum)is int and 0<len(raw)<=min(maximum,MAX_METADATA_BYTES),CODE)
    try:value=json.loads(raw.decode('utf-8'),object_pairs_hook=_closed_pairs)
    except (UnicodeError,ValueError):need(False,CODE)
    need(type(value)is dict and set(value)=={'format','fingerprints','metadata_derivations'}and value['format']==FORMAT,CODE)
    need(type(value['fingerprints'])is list and type(value['metadata_derivations'])is list,CODE)
    for row in value['fingerprints']:
        need(type(row)is dict and set(row)=={'logical_name','role','source_fingerprint','target_fingerprint','content_sha256','bytes'},CODE)
        need(row['role']in {'pdf','image','video'},CODE);_logical(row['logical_name'],row['role']);_hex(row['content_sha256'])
        source_size,source_ns=parse_registered_fingerprint(row['source_fingerprint']);target_size,target_ns=parse_registered_fingerprint(row['target_fingerprint'])
        need(type(row['bytes'])is int and 0<=source_ns<2**63 and source_size==target_size==row['bytes']
             and target_ns==source_ns-source_ns%100 and source_ns!=target_ns,CODE)
    for row in value['metadata_derivations']:
        need(type(row)is dict and set(row)=={'logical_name','source_sha256','target_sha256','source_bytes','target_bytes','source_projection_sha256','target_projection_sha256'},CODE)
        _logical(row['logical_name'],'metadata')
        for key in ('source_sha256','target_sha256','source_projection_sha256','target_projection_sha256'):_hex(row[key])
        need(all(type(row[key])is int and 0<row[key]<=MAX_METADATA_BYTES for key in ('source_bytes','target_bytes')),CODE)
    for key in ('fingerprints','metadata_derivations'):
        encoded=[canonical(row)for row in value[key]]
        need(encoded==sorted(set(encoded)),CODE)
    need(canonical(value)==raw,CODE)
    return value


def encode_provenance(incoming,fingerprints,metadata,maximum=MAX_METADATA_BYTES):
    values={'format':FORMAT,'fingerprints':[],'metadata_derivations':[]}
    for raw in incoming:
        decoded=decode_provenance(raw,maximum)
        for key in ('fingerprints','metadata_derivations'):values[key].extend(decoded[key])
    values['fingerprints'].extend(fingerprints);values['metadata_derivations'].extend(metadata)
    for key in ('fingerprints','metadata_derivations'):
        values[key]=[json.loads(raw)for raw in sorted({canonical(row)for row in values[key]})]
    raw=canonical(values);decode_provenance(raw,maximum)
    return raw


# The opaque reader exists only while its original owner/held ZIP scope is live.
import weakref
from contextlib import contextmanager
_DERIVED=weakref.WeakKeyDictionary()
class _DerivedSpool:pass


@contextmanager
def derivative_spool(work,plan,manifest,metadata_source,incoming,policy):
    """Three fixed owned work cores; all output bytes fixed before enrollment."""
    import os,zipfile
    from . import registered_asset_backup as reg,restore_publication as pub
    from .parent_restore_workflow import _SourceHandles
    from .parent_restore_assets import _OriginalArchiveFile
    from .registered_asset_snapshot import HeldFile,_open_readonly
    need(type(incoming)is dict,CODE)
    changed=[r for r in plan['references']if r['source_fingerprint']!=r['target_fingerprint']]
    if not changed and not plan['metadata_changes']and not incoming:
        yield None;return
    assets={item['logical_name']:item for item in manifest['assets']}
    maximum=min(policy.max_manifest_bytes,MAX_METADATA_BYTES)
    budget=sum(spec['source_bytes']*2 for spec in plan['metadata_changes'].values())
    budget+=sum(len(raw)for value in incoming.values()for raw in value['raws'])
    budget+=len(changed)*2048+len(plan['metadata_changes'])*4096+65536
    budget=min(budget,policy.max_total_bytes)
    need(0<budget<=2**53-1,CODE);reg.space(work.work,budget)
    expected={};records={};sources={};history={};source_inputs={};folded=set()
    def output(relative,name,sha,size,refs,source_logical=None):
        _logical(source_logical,'metadata')if source_logical else None
        need(name not in expected and name.casefold()not in folded,CODE);folded.add(name.casefold())
        if relative in records:
            prior=records[relative];need(prior['sha256']==sha and prior['bytes']==size,CODE)
            prior['references']=[json.loads(raw)for raw in sorted({canonical(x)for x in prior['references']+refs})]
        else:records[relative]={'relative':relative,'purpose':'metadata','child_id':None,'references':refs,'sha256':sha,'bytes':size}
        expected[name]={'relative':relative,'sha256':sha,'bytes':size}
        if source_logical:sources[source_logical]={'member_name':name,'relative':relative,'sha256':sha,'bytes':size}
    def ref(key,column):
        return {'table':'kg_corpus','id_column':'corpus_id','id':key,'column':column,'child_id':None}
    with work.open_fingerprint_spool_writer(max_bytes=budget)as file:
        with zipfile.ZipFile(file,'w',compression=zipfile.ZIP_STORED,allowZip64=True)as archive:
            for logical,spec in sorted(plan['metadata_changes'].items()):
                item=assets[logical]
                need(item['bytes']==spec['source_bytes']and item['sha256']==spec['source_sha256']
                    and 0<item['bytes']<=min(policy.max_file_bytes,MAX_METADATA_BYTES),CODE)
                target=work.reset_fingerprint_core('target')
                with metadata_source(logical,item)as source:
                    original_projection=metadata_projection(source)
                    with pub._sql(target,owner=work.owner,timeout=0)as destination:
                        projected=rewrite_metadata_connection(source,destination,spec['changes'])
                with _SourceHandles(work.owner)as held_scope:
                    held=held_scope.enter_context(HeldFile(target.parent,target.name,policy))
                    sha,size=held.hash();need(0<size<=min(policy.max_file_bytes,MAX_METADATA_BYTES),CODE)
                    need(not any(os.path.lexists(str(target)+suffix)for suffix in ('-wal','-shm','-journal')),CODE)
                    with pub._sql(target,owner=work.owner,readonly=True,timeout=0)as check:
                        check.execute('PRAGMA query_only=ON');check.execute('PRAGMA trusted_schema=OFF');verify_metadata_connection(check,projected)
                    name='metadata/'+digest(logical.encode('utf-8'))+'.db'
                    info=zipfile.ZipInfo(name);info.compress_type=zipfile.ZIP_STORED;info.external_attr=0o100600<<16
                    with archive.open(info,'w',force_zip64=True)as stream:
                        for block in held.chunks():stream.write(block)
                    need(held.hash()==(sha,size),CODE)
                output(spec['target_relative'],name,sha,size,[ref(spec['corpus_id'],'metadata_db_path')],logical)
                source_inputs[logical]=item['sha256']
                history.setdefault(spec['corpus_id'],[]).append({'logical_name':logical,
                    'source_sha256':item['sha256'],'target_sha256':sha,'source_bytes':item['bytes'],'target_bytes':size,
                    'source_projection_sha256':original_projection,'target_projection_sha256':projected})
            provenance,inputs=provenance_outputs(plan,incoming,history,policy)
            for logical,sha in inputs.items():
                need(logical not in source_inputs or source_inputs[logical]==sha,CODE);source_inputs[logical]=sha
            for relative,(raw,refs)in provenance.items():
                name='provenance/'+digest(relative.encode('utf-8'))+'.json'
                info=zipfile.ZipInfo(name);info.compress_type=zipfile.ZIP_STORED;info.external_attr=0o100600<<16
                archive.writestr(info,raw)
                output(relative,name,digest(raw),len(raw),refs)
    path=work.work/'fingerprint-derived.zip';value=work._value()
    entry=next(e for e in value['members']['entries']if work.owner.path.parent/e['relative']==path)
    with _SourceHandles(work.owner)as held_scope:
        held=held_scope.enter_context(HeldFile(path.parent,path.name,policy));whole=held.hash()
        need(whole[1]<=budget,CODE)
        reader=held_scope.enter_context(_OriginalArchiveFile(path,entry['identity']))
        archive=held_scope.enter_context(reg._finite_archive_file(reader,len(expected)))
        infos=archive.infolist()
        need(len(infos)==len(expected)and {i.filename for i in infos}==set(expected)
            and len({i.filename.casefold()for i in infos})==len(infos),CODE)
        for info in infos:
            need(not info.is_dir()and not info.flag_bits&1 and info.compress_type==zipfile.ZIP_STORED
                and info.compress_size==info.file_size==expected[info.filename]['bytes'],CODE)
        capability=_DerivedSpool();state={'active':True,'work':work,'held':held,'archive':archive,
            'expected':expected,'records':list(records.values()),'sources':sources,'source_inputs':source_inputs,'whole':whole}
        _DERIVED[capability]=state
        try:
            for name in expected:verify_derived_member(capability,name)
            yield capability
        finally:
            state['active']=False;need(held.hash()==whole,CODE);_DERIVED.pop(capability,None)


def _derived(value):
    need(type(value)is _DerivedSpool and value in _DERIVED,CODE);state=_DERIVED[value]
    need(state['active'],CODE);state['work']._value();state['held'].check()
    return state


def derived_contract(value):
    if value is None:return {'records':[],'sources':{},'source_inputs':{}}
    state=_derived(value)
    return json.loads(canonical({key:state[key]for key in ('records','sources','source_inputs')}))


def derived_blocks(value,name):
    state=_derived(value);need(type(name)is str and name in state['expected'],CODE)
    expected=state['expected'][name];sha=hashlib.sha256();size=0
    with state['archive'].open(name)as stream:
        while True:
            _derived(value);block=stream.read(65536)
            if not block:break
            size+=len(block);need(size<=expected['bytes'],CODE);sha.update(block);yield block
    need((sha.hexdigest(),size)==(expected['sha256'],expected['bytes']),CODE)
    _derived(value)


def verify_derived_member(value,name):
    for _ in derived_blocks(value,name):pass
    expected=_derived(value)['expected'][name]
    return expected['sha256'],expected['bytes']


def derived_member_for_relative(value,relative):
    state=_derived(value)
    names=sorted(name for name,item in state['expected'].items()if item['relative']==relative)
    need(names,CODE)
    first=state['expected'][names[0]]
    need(all(state['expected'][name]==first for name in names),CODE)
    return names[0]


MAIN_COLUMNS={'kg_resource':('resource_id','video_fingerprint'),
              'kg_document_page':('page_id','image_fingerprint')}


def main_rewrite_tuples(changes):
    need(type(changes)is list,CODE);seen={};result=[]
    keys={'table','id_column','id','column','corpus_id','resource_id','source_fingerprint','target_fingerprint','logical_name'}
    for row in changes:
        need(type(row)is dict and set(row)==keys and row['table']in MAIN_COLUMNS,CODE)
        id_column,column=MAIN_COLUMNS[row['table']]
        need((row['id_column'],row['column'])==(id_column,column),CODE)
        for key in ('id','corpus_id','resource_id'):need(type(row[key])is str and 0<len(row[key].encode('utf-8'))<=1024,CODE)
        metadata_changes([(row['id'],row['source_fingerprint'],row['target_fingerprint'])])
        _logical(row['logical_name'],'video'if row['table']=='kg_resource'else'image')
        if row['table']=='kg_resource':need(row['resource_id']==row['id'],CODE)
        identity=(row['table'],row['id']);need(identity not in seen or seen[identity]==row,CODE)
        if identity not in seen:result.append((row['table'],id_column,row['id'],column,row['target_fingerprint']))
        seen[identity]=row
    return result



CATALOG_TABLE='paper_scoring_source_catalog_revision'
CATALOG_SCHEMA="""CREATE TABLE paper_scoring_source_catalog_revision (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          revision INTEGER NOT NULL DEFAULT 0
        )"""


def _catalog_trigger_sql(table,event):
    need(table in MAIN_COLUMNS and event in {'INSERT','UPDATE','DELETE'},CODE)
    return f"""CREATE TRIGGER paper_scoring_catalog_{table}_{event.lower()}
                    AFTER {event} ON {table}
                    BEGIN
                      UPDATE paper_scoring_source_catalog_revision
                      SET revision=revision+1 WHERE singleton=1;
                    END"""


def catalog_revision_contract(connection,changes):
    """Only exact original cache-invalidating triggers may have their effect.

    Authenticated arbitrary trigger text is still untrusted. No trigger is
    disabled or re-created; absence in an older schema never invents an effect.
    """
    main_rewrite_tuples(changes);counts={};allowed=set();increments=0
    for row in changes:counts[row['table']]=counts.get(row['table'],0)+1
    for table,count in sorted(counts.items()):
        for name,sql in connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'AND tbl_name=? ORDER BY name",(table,)):
            matches=[event for event in ('INSERT','UPDATE','DELETE')if name=='paper_scoring_catalog_'+table+'_'+event.lower()
                     and sql==_catalog_trigger_sql(table,event)]
            need(len(matches)==1,CODE)
            if matches[0]=='UPDATE':allowed.add(name);increments+=count
    if allowed:
        schema=connection.execute("SELECT type,sql FROM sqlite_master WHERE name=?",(CATALOG_TABLE,)).fetchone()
        need(schema is not None and tuple(schema)==('table',CATALOG_SCHEMA),CODE)
        need(connection.execute("SELECT count(*)FROM sqlite_master WHERE type='trigger'AND tbl_name=?",(CATALOG_TABLE,)).fetchone()[0]==0,CODE)
        rows=connection.execute('SELECT singleton,revision,typeof(singleton),typeof(revision)FROM '+CATALOG_TABLE).fetchall()
        need(len(rows)==1 and tuple(rows[0])[0]==1 and tuple(rows[0])[2:]==('integer','integer')
             and type(rows[0][1])is int and 0<=rows[0][1]<=2**63-1-increments,CODE)
    return {'origins':allowed,'increments':increments}

def apply_main_fingerprint_changes(connection,changes):
    """Called only inside the original fixed-rewrite transaction/authorizer."""
    main_rewrite_tuples(changes)
    for row in changes:
        if row['table']=='kg_resource':
            result=connection.execute('UPDATE kg_resource SET video_fingerprint=? WHERE resource_id=? AND corpus_id=? AND video_fingerprint=?',
                (row['target_fingerprint'],row['id'],row['corpus_id'],row['source_fingerprint']))
        else:
            result=connection.execute('UPDATE kg_document_page SET image_fingerprint=? WHERE page_id=? AND resource_id=? AND image_fingerprint=? AND EXISTS(SELECT 1 FROM kg_resource r WHERE r.resource_id=kg_document_page.resource_id AND r.corpus_id=?)',
                (row['target_fingerprint'],row['id'],row['resource_id'],row['source_fingerprint'],row['corpus_id']))
        need(result.rowcount==1,CODE)


def incoming_plan(connection,manifest,policy,read_reserved):
    from .registered_asset_restore import _Plan,_columns
    from .registered_asset_snapshot import record_key
    original=_Plan(None,None,manifest,policy,None);seen=set();result={};groups={}
    if not _columns(connection,'kg_corpus'):
        need(not any(item['role']==ROLE for item in manifest['assets']),CODE);return result
    for row in original.rows(connection.execute('SELECT corpus_id,transcript_root FROM kg_corpus ORDER BY corpus_id')):
        key=row['corpus_id'];logical='registered/kg/'+record_key('kg',key)+'/'+ROLE+'/'+NAME
        if logical not in original.assets:continue
        item=original.assets[logical]
        need(item['kind']=='kg'and item['record_sha256']==record_key('kg',key)and item['role']==ROLE
             and item['relative_path']==NAME and item['storage']=='regular'
             and item['bytes']<=min(policy.max_manifest_bytes,MAX_METADATA_BYTES),CODE)
        raw=read_reserved(logical,item);need(type(raw)is bytes and (digest(raw),len(raw))==(item['sha256'],item['bytes']),CODE)
        decode_provenance(raw,min(policy.max_manifest_bytes,MAX_METADATA_BYTES))
        group=row['transcript_root'];need(group not in groups or groups[group]==raw,CODE);groups[group]=raw
        result[key]={'raws':[raw],'source_inputs':{logical:item['sha256']}};seen.add(logical)
    need(seen=={item['logical_name']for item in manifest['assets']if item['role']==ROLE},CODE)
    return result


def platform_precision_ns():
    import os
    return 100 if os.name=='nt'else 1


def provenance_outputs(plan,incoming,history,policy):
    """The same flattened, idempotent bytes for native and legacy preparation."""
    need(set(incoming)<=set(plan['corpus_groups'])and set(history)<=set(plan['corpus_groups']),CODE)
    groups={};outputs={};inputs={}
    changed=[r for r in plan['references']if r['source_fingerprint']!=r['target_fingerprint']]
    for key,group in plan['corpus_groups'].items():groups.setdefault(group,[]).append(key)
    for group,keys in sorted(groups.items()):
        old=[];fingerprints=[];derivations=[];refs=[]
        for key in sorted(keys):
            refs.append({'table':'kg_corpus','id_column':'corpus_id','id':key,'column':'transcript_root','child_id':None})
            if key in incoming:
                value=incoming[key];old.extend(value['raws'])
                for logical,sha in value['source_inputs'].items():
                    need(logical not in inputs or inputs[logical]==sha,CODE);inputs[logical]=sha
            for value in changed:
                if value['corpus_id']!=key:continue
                fingerprints.append({'logical_name':value['logical_name'],'role':value['role'],
                  'source_fingerprint':value['source_fingerprint'],'target_fingerprint':value['target_fingerprint'],
                  'content_sha256':value['source_sha256'],'bytes':value['bytes']})
            derivations.extend(history.get(key,()))
        if not old and not fingerprints and not derivations:continue
        raw=encode_provenance(old,fingerprints,derivations,min(policy.max_manifest_bytes,MAX_METADATA_BYTES))
        need(len(raw)<=policy.max_file_bytes,CODE);outputs[group+'/'+NAME]=(raw,refs)
    return outputs,inputs


def verify_export(connection,sql,resources,database,snapshot,export_erase=None):
    """Attach only original completed immutable-generation marker facts.

    Called by the existing original ExportTicket scope. The marker bytes are
    history, not file or SQL authority; provenance outside a verified completed
    generation does not opt an older source into this new contract.
    """
    import os
    from pathlib import Path
    from . import parent_restore_assets as assets
    from .registered_asset_snapshot import Policy,local_absolute,_open_readonly
    from .standard_relocation_provenance import _completed_generation,_verified_manifest,_EXPORT_SCOPE_CODE
    import sys
    need(sys._getframe(1).f_code is _EXPORT_SCOPE_CODE,CODE)
    policy=Policy(assets.MAX_FILES,64,1024,MAX_METADATA_BYTES,2**53-1,2**53-1)
    tables=assets.pub._tables(sql)
    if 'kg_corpus'not in tables:
        # A genuine older schema has no KG registry on either admitted image.
        # Absence never substitutes for completed generation/member authority.
        with _open_readonly(snapshot)as exported:
            need(not sql.execute("SELECT 1 FROM sqlite_master WHERE lower(name)='kg_corpus'").fetchone()
                 and not exported.execute("SELECT 1 FROM sqlite_master WHERE lower(name)='kg_corpus'").fetchone(),CODE)
            names=assets.CONTROL_TABLES
            query='SELECT type,name,sql FROM sqlite_master WHERE lower(name)IN('+','.join('?'for _ in names)+')ORDER BY type,name'
            live=[tuple(row)for row in sql.execute(query,names)]
            cold=[tuple(row)for row in exported.execute(query,names)]
            need(live==cold,CODE)
            if live:
                assets.validate_schema(sql);assets.validate_schema(exported)
                generation_sql=next(value for kind,name,value in live if kind=='table'and name==assets.GENERATION)
                expected=assets.CONTROL_TABLES if generation_sql==assets.GENERATION_SQL_88 else assets.LEGACY_CONTROL_TABLES
                need(all({(kind,name)for kind,name,_ in rows}=={('table',name)for name in expected}for rows in(live,cold)),CODE)
                for name in names:
                    if name in tables:
                        need(not sql.execute('SELECT 1 FROM '+name+' LIMIT 1').fetchone()
                             and not exported.execute('SELECT 1 FROM '+name+' LIMIT 1').fetchone(),CODE)
        return {}
    sources=[dict(row)for row in sql.execute('SELECT * FROM kg_corpus ORDER BY corpus_id LIMIT ?',(policy.max_files+1,))]
    need(len(sources)<=policy.max_files,CODE)
    with _open_readonly(snapshot)as exported:
        actual=[dict(row)for row in exported.execute('SELECT * FROM kg_corpus ORDER BY corpus_id LIMIT ?',(policy.max_files+1,))]
        need(actual==sources,CODE)
    verified={};cache={}
    if assets.GENERATION not in assets.pub._tables(sql):return verified
    generations=[dict(row)for row in sql.execute("SELECT * FROM restore_asset_generation WHERE state='ADOPTED' LIMIT ?",(policy.max_files+1,))]
    need(len(generations)<=policy.max_files,CODE)
    for source in sources:
        key=source['corpus_id'];value=source['transcript_root']
        if value in {'bundle:'+key,'local://family-textbook-ocr-pages'}:continue
        path=local_absolute(value,policy)
        matches=[g for g in generations if str(path).startswith(str(database.path.parent/'media'/g['root_name'])+os.sep)]
        need(len(matches)<=1,CODE)
        if not matches:continue
        generation=matches[0];root=database.path.parent/'media'/generation['root_name'];relative=path.relative_to(root).as_posix()+'/'+NAME
        rows=sql.execute('SELECT * FROM restore_asset_member WHERE generation_id=? AND relative_name=?',(generation['generation_id'],relative)).fetchall()
        need(len(rows)<=1,CODE)
        if not rows:continue
        member=dict(rows[0])
        try:refs=json.loads(member['reference_json'])
        except(ValueError,TypeError):need(False,CODE)
        shape=(member['kind']=='file'and member['purpose']=='metadata'and member['child_id']is None
               and type(refs)is list and refs and all(type(ref)is dict and set(ref)=={'table','id_column','id','column','child_id'}
                 and ref['table']=='kg_corpus'and ref['id_column']=='corpus_id'and ref['column']=='transcript_root'
                 and ref['child_id']is None and type(ref['id'])is str for ref in refs))
        if not shape:continue  # Old legitimate same-basename members keep their original export behavior.
        need(member['state']=='READY'and 0<member['expected_bytes']<=min(policy.max_manifest_bytes,MAX_METADATA_BYTES),CODE)
        enrolled={ref['id']for ref in refs};need(len(enrolled)==len(refs)and key in enrolled,CODE)
        same_root={row['corpus_id']for row in sources if row['transcript_root']==value};need(same_root<=enrolled,CODE)
        cache_key=(generation['generation_id'],str(path))
        if cache_key not in cache:
            _completed_generation(sql,generation)
            cache[cache_key]=_verified_manifest(sql,resources,database,generation,path,export_erase)
        _,members,held_files=cache[cache_key]
        need(relative in members and relative in held_files,CODE)
        held=held_files[relative];raw=b''.join(held.chunks());decode_provenance(raw,policy.max_manifest_bytes)
        expected=(member['expected_sha256'],member['expected_bytes']);need(held.hash()==expected,CODE)
        verified[key]=(value,held,expected[0],expected[1])
    return verified


def authorize_export(ticket,snapshot,key,root):
    """Consume the existing original live ticket; never mint a new capability."""
    from pathlib import Path
    from . import standard_relocation_provenance as original
    from .original_connection_read import _state
    from .registered_asset_snapshot import _PROVENANCE_COLLECTOR_CODE,_PROVENANCE_SNAPSHOT_CODE
    from .registered_asset_backup import _PROVENANCE_CAPSULE_CODE
    from .privacy_lifecycle import _SOURCE_EXPORT_PRODUCER_CODE
    import sys
    frame=sys._getframe(1)
    for code in(_PROVENANCE_COLLECTOR_CODE,_PROVENANCE_SNAPSHOT_CODE,_PROVENANCE_CAPSULE_CODE,_SOURCE_EXPORT_PRODUCER_CODE):
        need(frame is not None and frame.f_code is code,CODE);frame=frame.f_back
    need(type(ticket)is original._ExportTicket and ticket in original._EXPORTS,CODE)
    state=original._EXPORTS[ticket];connection=state['connection']();database=state['database']()
    need(state['active']and connection is not None and database is not None and connection.in_transaction
         and Path(snapshot)==state['snapshot']and 'fp_verified'in state,CODE)
    _state(connection,database);state['original'].check()
    verified=state['fp_verified']
    if key not in verified:return None
    expected_root,held,sha,size=verified[key]
    need(expected_root==str(root)and held.hash()==(sha,size),CODE)
    return sha,size
