"""Bounded relocation data. Bytes/rows never confer publication or file rights."""
from __future__ import annotations

import hashlib
import json
import re

NAME = '.hsc-standard-relocation-v1.json'
ROLE = 'relocation_provenance'
FORMAT = 'hsc_standard_relocation_v1'
MAX_ENTRIES = 4096
MAX_BYTES = 4 * 1024 * 1024
CODE = 'STANDARD_RELOCATION_PROVENANCE_UNCONFIRMED'
_HEX = re.compile(r'[0-9a-f]{64}')


class StandardRelocationError(ValueError):
    pass


def need(value):
    if not value:
        raise StandardRelocationError(CODE)


def canonical(value):
    # This is deliberately the original paired-paper binding encoding.
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        need(key not in result)
        result[key] = value
    return result


def _entry(value):
    from .registered_asset_restore import _source_path
    from .registered_asset_snapshot import Policy
    need(type(value) is dict and set(value) == {'source_id', 'old_root', 'old_source_sha256'})
    need(type(value['source_id']) is str and 1 <= len(value['source_id'].encode('utf-8')) <= 1024)
    need(type(value['old_source_sha256']) is str and _HEX.fullmatch(value['old_source_sha256']))
    # Validate lexical syntax only. In particular, do not normalize the string
    # that the original complete source row included in its hash.
    _source_path(value['old_root'], Policy(100000, 64, 1024, 64*1024*1024, 2**53-1, 2**53-1))
    return {key: value[key] for key in ('source_id', 'old_root', 'old_source_sha256')}


def encode(entries):
    need(type(entries) is list and 0 < len(entries) <= MAX_ENTRIES)
    values = [_entry(value) for value in entries]
    ordered = sorted(values, key=lambda value: (value['source_id'], value['old_root'], value['old_source_sha256']))
    need(values == ordered and len({(v['source_id'], v['old_root']) for v in values}) == len(values))
    raw = canonical({'format': FORMAT, 'entries': values})
    need(len(raw) <= MAX_BYTES)
    return raw


def decode(raw):
    need(type(raw) is bytes and 1 <= len(raw) <= MAX_BYTES)
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(StandardRelocationError(CODE)))
        need(type(value) is dict and set(value) == {'format', 'entries'} and value['format'] == FORMAT)
        need(encode(value['entries']) == raw)
        return value['entries']
    except (UnicodeError, TypeError, KeyError, RecursionError, json.JSONDecodeError):
        raise StandardRelocationError(CODE) from None


def revalidate(entries, sources):
    """Authenticated incoming data must explain only the root field, nothing else."""
    need(type(sources) is dict and 0 < len(sources) <= MAX_ENTRIES)
    for entry in entries:
        need(entry['source_id'] in sources)
        current = dict(sources[entry['source_id']])
        current['local_root_path'] = entry['old_root']
        need(digest(current) == entry['old_source_sha256'])
    return entries


def referenced_entries(c, sources, incoming=()):
    """Pure derivation on the authenticated Cold DB; never opens an old root."""
    candidates = list(incoming)
    revalidate(candidates, sources)
    for source in sources.values():
        entry = {'source_id': source['source_id'], 'old_root': source['local_root_path'],
                 'old_source_sha256': digest(dict(source))}
        if entry not in candidates:
            candidates.append(entry)
    by_source = {}
    for entry in candidates:
        by_source.setdefault(entry['source_id'], []).append(entry)
    wanted = set()
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_event'").fetchone():
        return []
    rows = c.execute("SELECT child_id, CASE WHEN typeof(summary_json)='text' AND "
        "length(CAST(summary_json AS BLOB)) BETWEEN 1 AND ? THEN summary_json ELSE NULL END AS summary_json "
        "FROM audit_event WHERE actor='parent' AND action='parent_paired_paper_objective_submitted' LIMIT ?",
        (MAX_BYTES, MAX_ENTRIES+1)).fetchall()
    need(len(rows) <= MAX_ENTRIES)
    for row in rows:
        need(row['summary_json'] is not None)
        try:
            receipt = json.loads(row['summary_json'], object_pairs_hook=_pairs)
            binding = receipt['binding']; standard = binding['standard']
            requirement = c.execute('SELECT * FROM curriculum_standard_requirement WHERE requirement_id=?',
                                    (standard['requirement_id'],)).fetchone()
            need(requirement is not None)
            source_id = requirement['source_id']
            if source_id not in sources:
                continue
            need(binding['child_id'] == row['child_id'])
            page = c.execute('SELECT * FROM curriculum_standard_page WHERE source_id=? AND page_number=?',
                             (source_id, standard['standard_page'])).fetchone()
            need(page is not None)
            linked = (c.execute('SELECT * FROM curriculum_manual_requirement WHERE requirement_id=?',
                                (standard['requirement_id'],)).fetchone()
                      if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='curriculum_manual_requirement'").fetchone() else None)
            if linked is not None:
                need(linked['child_id'] == row['child_id'])
            for entry in by_source[source_id]:
                old = dict(sources[source_id]); old['local_root_path'] = entry['old_root']
                if digest([dict(requirement), old, dict(page), dict(linked) if linked is not None else None]) == standard['binding']:
                    wanted.add((entry['source_id'], entry['old_root'], entry['old_source_sha256']))
        except (TypeError, KeyError, ValueError, RecursionError):
            raise StandardRelocationError(CODE) from None
    result = [{'source_id': source, 'old_root': root, 'old_source_sha256': sha}
              for source, root, sha in sorted(wanted)]
    if result:
        encode(result)
    return result


def _verified_manifest(sql, resources, database, generation, standard_root, export_erase=None):
    """Verify the original namespace/seal and current target material bytes.

    Unrelated content is not consumed: its original IDs, size/mtime and sealed
    expected SHA are checked sequentially, without retaining every household
    file handle. Completed erasures require the existing immutable facts.
    """
    import os
    from . import parent_restore_assets as assets
    from .registered_asset_snapshot import _held_directory, HeldFile, held_registered_fingerprint
    rows, names, policy = assets._persistent_members(sql, generation)
    root = database.path.parent/'media'/generation['root_name']
    root_id = json.loads(generation['root_identity_json'])
    media_id = json.loads(generation['media_identity_json'])
    from .original_connection_read import _state, _ReadResources
    from . import restore_publication as pub
    state,_,admission=_state(sql.connection,database)
    original_parent=pub._original_read_lease(admission,database.path)['parent']
    assets._private_original_ancestor(original_parent)
    def acl(directory,expected,scope):
        if os.name=='nt':
            from .money_r4.money_files import _windows_kernel
            from .registered_asset_restore import _win_target_check
            kernel,security=_windows_kernel();handle=kernel.guarded_open(directory.path,directory=True)
            scope.callback(kernel.close,handle)
            _win_target_check(kernel,handle,directory.path,directory=True,identity=tuple(expected))
            security.check(handle)
            _win_target_check(kernel,handle,directory.path,directory=True,identity=tuple(expected))
        directory.check()
    media = resources.enter_context(_held_directory(root.parent, policy))
    need(list(media.identity) == media_id);acl(media,media_id,resources)
    parent = resources.enter_context(_held_directory(root, policy, owned=True, ancestor=media))
    need(list(parent.identity) == root_id);acl(parent,root_id,resources)
    seen = set(); fingerprints = {}; held_files = {}
    def current_file(held,row,name,consume):
        held.check()
        actual = list(held.before['identity']) if os.name == 'nt' else [held.before.st_dev, held.before.st_ino]
        need(actual==json.loads(row['identity_json']) and held.verified_length()==row['expected_bytes'])
        if consume:
            need(held.hash()==(row['expected_sha256'],row['expected_bytes']))
            held_files[name]=held
        fingerprints[name]=held_registered_fingerprint(held)
        held.check()
    def walk(directory, relative):
        with os.scandir(assets.io_path(directory.path if os.name == 'nt' else directory.fd)) as children:
            for child in children:
                name = relative+'/'+child.name if relative else child.name
                need(name in names and name not in seen); seen.add(name)
                row = names[name]
                need(row['state'] == 'READY' and row['identity_json'] is not None)
                identity = json.loads(row['identity_json'])
                if row['kind'] == 'directory':
                    path=directory.path/child.name
                    # Only the target subtree and its original ancestor chain
                    # need to stay held throughout the business verification.
                    if path==standard_root or standard_root.is_relative_to(path) or path.is_relative_to(standard_root):
                        held=resources.enter_context(_held_directory(path,policy,ancestor=directory))
                        need(list(held.identity)==identity);acl(held,identity,resources);walk(held,name)
                    else:
                        with _ReadResources(admission)as scope:
                            held=scope.enter_context(_held_directory(path,policy,ancestor=directory))
                            need(list(held.identity)==identity);acl(held,identity,scope);walk(held,name)
                else:
                    if (root/name).is_relative_to(standard_root):
                        held=resources.enter_context(HeldFile(root,name,policy))
                        current_file(held,row,name,True)
                    else:
                        with _ReadResources(admission)as scope:
                            held=scope.enter_context(HeldFile(root,name,policy))
                            current_file(held,row,name,False)
                directory.check()
    walk(parent, '')
    need(seen == {name for name, row in names.items() if row['state'] == 'READY'})
    parent_cache = {}
    for row in rows:
        if row['state'] == 'REMOVED' and row['purpose'] == 'managed':
            if export_erase is not None:
                from .parent_restore_privacy import export_erased_preimage
                fingerprint=export_erased_preimage(export_erase,database,sql,generation,row,root)
                if fingerprint is not None:
                    fingerprints[row['relative_name']]=fingerprint
                    continue
            record = sql.execute('SELECT 1 FROM restore_asset_parent_erasure WHERE generation_id=? AND member_id=?',
                                 (generation['generation_id'], row['member_id'])).fetchone()
            if record is not None:
                from .parent_restore_privacy import completed_preimage
                fingerprint = completed_preimage(sql, generation, row, root, cache=parent_cache)
            else:
                fingerprint = assets._completed_erasure_preimage(sql, generation, row, root)
            fingerprints[row['relative_name']] = fingerprint
    need(assets._manifest_digest(generation, rows, fingerprints, root_id, media_id) == generation['manifest_sha256'])
    parent.check(); media.check()
    return root, names, held_files


def _completed_generation(sql, generation):
    from . import parent_restore_assets as assets, parent_restore_ownership as ownership
    from . import restore_publication as pub
    assets.validate_schema(sql)
    need(generation['state'] == 'ADOPTED')
    anchor = ownership._anchor(sql)
    need(anchor is not None)
    same_anchor=anchor['request_id']==generation['request_id']
    if same_anchor:
        need(anchor['state']=='DONE')
        binding=anchor['initial']['binding']
        need(assets._digest(pub._canonical(binding))==generation['binding_sha256']
             and pub._generation_matches(generation,binding)and binding['source_sha256']==generation['source_sha256'])
    else:
        # Existing historical-generation contract: a later original restore
        # owns the ACTIVE anchor. This is no permission by itself; the older
        # generation still needs its original finished authority and full seal.
        need(anchor['state']=='ACTIVE')
    receipt, _, authority, metadata = assets._generation_facts(sql, generation)
    need(receipt['state'] == 'finished' and receipt['finalized'] == 1
         and pub._authority_status(authority) == 'finished'
         and(not same_anchor or pub._metadata_matches(binding,metadata))
         and receipt['request_id']==metadata['request_id']==generation['request_id']
         and receipt['kind']==metadata['kind']and pub._generation_matches(generation,pub._authority_binding(metadata))
         and receipt['source_sha256'] == generation['source_sha256']
         and receipt['reference_type'] == 'audit_event' and receipt['reference_id'] is not None)
    # The original finished restore audit, not an arbitrary parent event.
    audit = sql.execute("SELECT actor,action,CASE WHEN typeof(summary_json)='text' AND "
        "length(CAST(summary_json AS BLOB)) BETWEEN 1 AND 8192 THEN summary_json ELSE NULL END AS summary_json "
        "FROM audit_event WHERE audit_id=?", (receipt['reference_id'],)).fetchone()
    need(audit is not None and audit['actor'] == 'parent' and audit['action'] ==
         {'restore': 'backup_restored', 'portable_restore': 'portable_backup_restored'}[receipt['kind']])
    summary = pub._json_object(audit['summary_json'], 8192, CODE)
    need(summary.get('privacy_operation_request_id') == generation['request_id'])


def _metadata_root(sql,resources,db,source,cache,export_erase=None):
    import os
    from pathlib import Path
    from . import parent_restore_assets as assets
    current = sql.execute('SELECT * FROM curriculum_standard_source WHERE source_id=?',
                          (source['source_id'],)).fetchone()
    need(current is not None and dict(current) == source)
    path = Path(source['local_root_path'])
    need(str(path)==source['local_root_path'])
    generations = sql.execute("SELECT * FROM restore_asset_generation WHERE state='ADOPTED' LIMIT ?",
                              (assets.MAX_FILES+1,)).fetchall()
    need(len(generations) <= assets.MAX_FILES)
    matches = [dict(g) for g in generations if str(path).startswith(str(db.path.parent/'media'/g['root_name'])+os.sep)]
    need(len(matches) == 1); generation = matches[0]
    _completed_generation(sql, generation)
    key=(generation['generation_id'],str(path))
    if key not in cache:cache[key]=_verified_manifest(sql,resources,db,generation,path,export_erase)
    root,members,held_files=cache[key]
    relative = path.relative_to(root).as_posix()+'/'+NAME
    need(relative in members and relative in held_files)
    member = members[relative]
    need(member['kind'] == 'file' and member['purpose'] == 'metadata' and member['state'] == 'READY'
         and member['child_id'] is None and member['expected_bytes'] <= MAX_BYTES)
    references = json.loads(member['reference_json'])
    expected = {'table': 'curriculum_standard_source', 'id_column': 'source_id', 'id': source['source_id'],
                'column': 'local_root_path', 'child_id': None}
    need(0<len(references)<=MAX_ENTRIES and len({ref['id']for ref in references})==len(references))
    need(all(type(ref) is dict and set(ref) == set(expected) and ref['table'] == expected['table']
             and ref['id_column'] == expected['id_column'] and ref['column'] == expected['column']
             and ref['child_id'] is None for ref in references))
    held = held_files[relative]; raw = b''.join(held.chunks()); held.check()
    need(len(raw) == member['expected_bytes'] and hashlib.sha256(raw).hexdigest() == member['expected_sha256'])
    entries = decode(raw)
    enrolled = {ref['id'] for ref in references}
    need(all(entry['source_id'] in enrolled for entry in entries))
    return entries,held,enrolled


def _source_metadata(sql,resources,db,source,cache):
    entries,held,enrolled=_metadata_root(sql,resources,db,source,cache)
    need(source['source_id']in enrolled)
    selected=[entry for entry in entries if entry['source_id']==source['source_id']]
    revalidate(selected,{source['source_id']:source})
    return entries,held,selected


def old_roots(connection, source, database=None):
    """Finite explanations for this exact current source; no healthy-path fallback."""
    from .original_connection_read import _verification
    with _verification(connection,database)as(sql,resources,db):
        entries,held,selected=_source_metadata(sql,resources,db,source,{})
        return tuple(entry['old_root']for entry in selected)


from contextvars import ContextVar
from contextlib import contextmanager
import sqlite3
from uuid import uuid4
_BUSINESS_SCOPES=ContextVar('original_business_read_scopes',default=())
_SQL_CURSOR=sqlite3.Connection.cursor
_SQL_CURSOR_CLASS=sqlite3.Cursor
_SQL_ROLLBACK=sqlite3.Connection.rollback


def business_snapshot_active(connection):
    from . import restore_publication as pub
    return any(original()is connection and actor==pub._actor()for original,name,actor in _BUSINESS_SCOPES.get())


def _read_statement(connection,sql):
    cursor=_SQL_CURSOR(connection,factory=_SQL_CURSOR_CLASS)
    try:cursor.execute(sql)
    finally:
        try:cursor.close()
        except BaseException:
            from .original_connection_read import poison_original
            poison_original(connection,('original_business_cursor',cursor));raise


@contextmanager
def business_snapshot(connection):
    """Business-owned nested marker; capability verification never controls SQL.

    A no-write ROLLBACK/COMMIT followed by BEGIN loses the private original
    marker. RELEASE must succeed before a proof/export can return. A borrowed
    transaction is never committed or rolled back by this scope.
    """
    import weakref
    from . import restore_publication as pub
    from .original_connection_read import poison_original
    from .original_connection_read import original_connection
    supplied=connection;connection=original_connection(supplied)
    if business_snapshot_active(connection):
        try:yield connection
        finally:need(original_connection(supplied)is connection)
        return
    started=not connection.in_transaction
    if started:_read_statement(connection,'BEGIN')
    marker='hsc_original_read_'+uuid4().hex
    try:_read_statement(connection,'SAVEPOINT '+marker)
    except BaseException:
        poison_original(connection,('original_business_marker_setup',connection));raise
    try:original=weakref.ref(connection)
    except TypeError:original=lambda:connection  # Existing raw SQLite healthy reader, no file capability.
    token=_BUSINESS_SCOPES.set((*_BUSINESS_SCOPES.get(),(original,marker,pub._actor())))
    body_error=None;confirmed=False
    try:
        try:
            yield connection
            need(original_connection(supplied)is connection)
        except BaseException as error:body_error=error;raise
    finally:
        try:
            try:
                _read_statement(connection,'RELEASE SAVEPOINT '+marker);confirmed=True
            except BaseException as error:
                poison_original(connection,('original_business_marker_lost',connection))
                if not isinstance(error,Exception):raise
                if isinstance(body_error,pub.RestorePublicationError)and str(body_error)=='RESTORE_PUBLICATION_CLOSE_UNCONFIRMED':
                    raise body_error from None
                raise pub.RestorePublicationError('RESTORE_PUBLICATION_SNAPSHOT_UNCONFIRMED')from None
            finally:
                # Once the marker is lost the current transaction is unknown;
                # do not take over a new transaction created by someone else.
                if started and confirmed:
                    try:_SQL_ROLLBACK(connection)
                    except BaseException:
                        poison_original(connection,('original_business_read_rollback',connection));raise
        finally:_BUSINESS_SCOPES.reset(token)


def snapshot_read(function):
    """Cover the complete original business evidence block in one snapshot."""
    from functools import wraps
    @wraps(function)
    def read(connection,*args,**kwargs):
        from .original_connection_read import registered_view,recording_source_view,_UnrecordedSourceRead
        if registered_view(connection)and recording_source_view(connection):
            # Do not admit transaction/schema commands into the recording's
            # SELECT-only authorizer. Its original owner retries the complete
            # read outside recording under the same business marker instead.
            raise _UnrecordedSourceRead()
        with business_snapshot(connection)as original:
            if original is not connection:
                # C-level proof reads do not pretend to populate the calibration
                # transcript. Full/live validation runs; this hint cannot cache.
                from .rubric_calibration_transaction import unsupported_dependency
                unsupported_dependency()
            return function(original,*args,**kwargs)
    return read


def generation_plan(c, manifest, policy, read_reserved):
    """Derive exact metadata before allocation from a held authenticated source.

    read_reserved is the existing opaque authenticated archive reader, not an
    arbitrary source-machine filename. This function itself grants no rights.
    """
    from .registered_asset_restore import registered_root
    from .registered_asset_snapshot import record_key
    groups = {}; seen_sources = set()
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='curriculum_standard_source'").fetchone():
        need(not any(item['role'] == ROLE for item in manifest['assets']))
        return {}, {}
    for source in c.execute('SELECT * FROM curriculum_standard_source ORDER BY source_id LIMIT ?', (100001,)):
        source = dict(source); need(source['source_id'] not in seen_sources); seen_sources.add(source['source_id'])
        need(len(seen_sources) <= 100000)
        group = registered_root('standard', source['local_root_path'], policy)
        groups.setdefault(group, {})[source['source_id']] = source
    assets = {item['logical_name']: item for item in manifest['assets']}
    outputs = {}; inputs = {}
    for group, sources in groups.items():
        incoming = []; raw_group = None; present = 0
        for source_id in sources:
            name = 'registered/standard/'+record_key('standard', source_id)+'/'+ROLE+'/'+NAME
            if name not in assets:
                continue
            present += 1; item = assets[name]
            need(item['kind'] == 'standard' and item['role'] == ROLE and item['relative_path'] == NAME
                 and item['storage'] == 'regular' and item['bytes'] <= MAX_BYTES)
            raw = read_reserved(name)
            need(type(raw) is bytes and len(raw) == item['bytes']
                 and hashlib.sha256(raw).hexdigest() == item['sha256'])
            if raw_group is None:
                raw_group = raw; incoming = decode(raw)
            else:
                need(raw == raw_group)
            inputs[name] = hashlib.sha256(raw).hexdigest()
        entries = referenced_entries(c, sources, incoming)
        if not entries:
            continue
        raw = encode(entries)
        references = [{'table': 'curriculum_standard_source', 'id_column': 'source_id', 'id': source_id,
                       'column': 'local_root_path', 'child_id': None} for source_id in sorted({entry['source_id']for entry in entries})]
        need(len(canonical(references))<=4096)
        outputs[group+'/'+NAME] = (raw, references)
    need({name for name, item in assets.items() if item['role'] == ROLE} == set(inputs))
    return outputs, inputs


def reserved_present(root, policy):
    """A reserved case alias is an error, never a silent missing metadata file."""
    import os
    from .registered_asset_snapshot import _held_directory
    from .backup_local_io import io_path
    with _held_directory(root, policy) as directory:
        matches=[]
        with os.scandir(io_path(root if os.name=='nt'else directory.fd))as children:
            for count,child in enumerate(children,1):
                need(count<=policy.max_files)
                if child.name.casefold()==NAME.casefold():matches.append(child.name)
                need(len(matches)<=1)
        directory.check()
        need(matches in ([], [NAME]))
        return bool(matches)


# Only a live upper export scope retains this object. Values contain weak
# references to original connections/DB and the live resources owner.
import weakref
from contextlib import contextmanager
_EXPORTS = weakref.WeakKeyDictionary()


class _ExportTicket:
    __slots__ = ('__weakref__',)
    def __new__(cls):
        raise StandardRelocationError(CODE)


@contextmanager
def export_scope(connection, database, snapshot):
    """Original admitted read txn and held normalized backup to capsule closure."""
    from pathlib import Path
    from .original_connection_read import _verification
    from .registered_asset_snapshot import Policy, HeldFile, _open_readonly, local_absolute
    policy = Policy(100000,64,1024,64*1024*1024,2**53-1,2**53-1)
    snapshot=local_absolute(str(snapshot),policy)
    import sys
    from .privacy_lifecycle import _SOURCE_EXPORT_PRODUCER_CODE
    # __enter__ is the sole intermediary; a public with-body caller cannot
    # manufacture or prolong an export ticket around an arbitrary snapshot.
    need(sys._getframe(2).f_code is _SOURCE_EXPORT_PRODUCER_CODE)
    with _verification(connection,database)as(sql,resources,db):
        producer=sys._getframe(2)
        export_erase=None
        if producer.f_locals.get('reason')=='post_deletion'and getattr(producer.f_locals.get('self'),'database',None)is db:
            from .parent_restore_privacy import current,validate_export_read
            export_erase=current(db)
            if export_erase is not None:validate_export_read(export_erase,db,sql)
        sources=[dict(row)for row in sql.execute('SELECT * FROM curriculum_standard_source ORDER BY source_id LIMIT ?', (100001,))]
        need(len(sources)<=100000)
        original=resources.enter_context(HeldFile(snapshot.parent,snapshot.name,policy));expected=original.hash()
        with _open_readonly(snapshot)as exported:
            actual=[dict(row)for row in exported.execute('SELECT * FROM curriculum_standard_source ORDER BY source_id LIMIT ?', (100001,))]
            need(actual==sources)
        verified={};cache={}
        for source in sources:
            root=local_absolute(source['local_root_path'],policy)
            if reserved_present(root,policy):
                entries,held,enrolled=_metadata_root(sql,resources,db,source,cache,export_erase)
                same_root={row['source_id']:row for row in sources if row['local_root_path']==source['local_root_path']}
                # Missing enrolled source facts cannot be inferred from SQL
                # absence: without original completed erasure this stays HOLD.
                revalidate(entries,same_root)
                raw=b''.join(held.chunks());held.check()
                verified[source['source_id']]=(source,held,hashlib.sha256(raw).hexdigest(),len(raw),source['source_id']in enrolled)
        from .registered_fingerprint_relocation import verify_export as verify_fingerprint_export
        fp_verified=verify_fingerprint_export(connection,sql,resources,db,snapshot,export_erase)
        value=object.__new__(_ExportTicket)
        state={'active':True,'connection':weakref.ref(connection),'database':weakref.ref(db),
               'snapshot':snapshot,'original':original,'expected':expected,'verified':verified,'fp_verified':fp_verified}
        _EXPORTS[value]=state
        try:
            yield value
            need(original.hash()==expected)
            for source,held,sha,size,enrolled in verified.values():need(held.hash()==(sha,size))
            for root,held,sha,size in fp_verified.values():need(held.hash()==(sha,size))
            if export_erase is not None:validate_export_read(export_erase,db,sql)
        finally:
            state['active']=False
            _EXPORTS.pop(value,None)


def require_original_erase_export(sql,database):
    """Fixed original caller chain, not a caller-supplied export flag."""
    import sys
    from .privacy_lifecycle import _SOURCE_EXPORT_PRODUCER_CODE
    frame=sys._getframe(2)
    for code in(_MANIFEST_READ_CODE,_METADATA_ROOT_CODE,_EXPORT_SCOPE_CODE):
        need(frame is not None and frame.f_code is code);last=frame;frame=frame.f_back
    need(last.f_locals.get('connection')is sql.connection and last.f_locals.get('database')is database)
    producer=frame.f_back if frame is not None else None
    need(producer is not None and producer.f_code is _SOURCE_EXPORT_PRODUCER_CODE
         and producer.f_locals.get('reason')=='post_deletion'
         and getattr(producer.f_locals.get('self'),'database',None)is database)


def authorize_export(ticket, snapshot, source_id, root):
    """Collector can consume an upper ticket; it cannot mint one from DB rows."""
    from pathlib import Path
    from .original_connection_read import _state
    import sys
    from .registered_asset_snapshot import _PROVENANCE_COLLECTOR_CODE,_PROVENANCE_SNAPSHOT_CODE
    from .registered_asset_backup import _PROVENANCE_CAPSULE_CODE
    from .privacy_lifecycle import _SOURCE_EXPORT_PRODUCER_CODE
    frame=sys._getframe(1)
    for code in(_PROVENANCE_COLLECTOR_CODE,_PROVENANCE_SNAPSHOT_CODE,
                _PROVENANCE_CAPSULE_CODE,_SOURCE_EXPORT_PRODUCER_CODE):
        need(frame is not None and frame.f_code is code);frame=frame.f_back
    need(type(ticket)is _ExportTicket and ticket in _EXPORTS)
    state=_EXPORTS[ticket];connection=state['connection']();db=state['database']()
    need(state['active']and connection is not None and db is not None and connection.in_transaction
         and Path(snapshot)==state['snapshot']and source_id in state['verified'])
    _state(connection,db);state['original'].check()
    source,held,sha,size,enrolled=state['verified'][source_id]
    need(source['local_root_path']==str(root));held.check()
    return (sha,size)if enrolled else None

_READ_ROOTS_CODE=old_roots.__code__
_EXPORT_SCOPE_CODE=export_scope.__wrapped__.__code__
_MANIFEST_READ_CODE=_verified_manifest.__code__
_METADATA_ROOT_CODE=_metadata_root.__code__
