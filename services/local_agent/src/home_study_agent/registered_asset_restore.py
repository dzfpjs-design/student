"""Prepare one owned registered-asset generation; never publish the live DB.

Caller has already authenticated the archive/AEAD contract and owns the target
restore transaction. Original source roots are identities only and are never
opened. The returned prepared database is for the parent's single DB publish.
"""
from __future__ import annotations
from .backup_local_io import io_path, io_resolve, io_walk, io_glob, io_mkdtemp, io_temporary_directory

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sqlite3
import stat
from uuid import uuid4

from .registered_asset_snapshot import (
    Policy, HeldFile, canonical, digest, record_key, safe_relative, local_absolute,
    verify_registered_asset_snapshot, _rows, _columns, _open_readonly, _CheckedWin32,
    parse_registered_fingerprint, registered_fingerprint_witness, held_registered_fingerprint,
)
from .pet_media_files import _windows_parts


class RegisteredAssetRestoreError(ValueError):
    """Finite codes. Caller-private owned_generation may describe cleanup debt."""


def need(value, code):
    if not value:
        raise RegisteredAssetRestoreError(code)


def _sha(value):
    need(type(value) is str and re.fullmatch('[0-9a-f]{64}', value), 'REGISTERED_RESTORE_BINDING_REQUIRED')
    return value


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, 'REGISTERED_RESTORE_DUPLICATE_FIELD')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def _source_path(value, policy):
    # Pure parsing only. Never resolve, stat, open or create an old root.
    need(type(value) is str and value and '\0' not in value, 'REGISTERED_RESTORE_ROOT_REQUIRED')
    if re.match(r'^[A-Za-z]:[\\/]', value):
        p = PureWindowsPath(value)
        need(p.is_absolute() and not str(p).startswith('\\\\'), 'REGISTERED_RESTORE_ROOT_REQUIRED')
    else:
        need(value.startswith('/') and not value.startswith('//') and '\\' not in value,
             'REGISTERED_RESTORE_ROOT_REQUIRED')
        p = PurePosixPath(value)
        need(str(p) == value, 'REGISTERED_RESTORE_ROOT_REQUIRED')
    need(len(p.parts) > 1, 'REGISTERED_RESTORE_ROOT_REQUIRED')
    safe_relative('/'.join(p.parts[1:]), policy)
    return p


def registered_root(kind,value,policy):
    """Original pure Plan root spelling; never opens an old machine path."""
    p = _source_path(value, policy)
    identity = str(p).casefold() if isinstance(p, PureWindowsPath) else str(p)
    return 'roots/' + digest(canonical([kind, identity]))


class _Plan:
    """Recompute authority from the closed DB and verified copied metadata only."""
    def __init__(self, database, payload, manifest, policy, final,*,parent_payloads=None,fingerprint_relocation=None,derived_outputs=None):
        self.database = database; self.payload = payload; self.manifest = manifest
        self.policy = policy; self.final = final; self.coverage = []; self.groups = {}
        self.parent_payloads=parent_payloads
        self.fingerprint_relocation=fingerprint_relocation
        self.derived_outputs=derived_outputs or {'records':[],'sources':{},'source_inputs':{}}
        self.derived_targets=set()
        self.provenance_inputs={}
        self.assets = {a['logical_name']: a for a in manifest['assets']}
        self.by_record = {}
        for name, item in self.assets.items():
            self.by_record.setdefault((item['kind'], item['record_sha256']), []).append(name)
        self.claimed = {}; self.versions = {}; self.rewrites = []; self.managed = set(); self.mtimes = {}

    def rows(self, rows):
        for number, row in enumerate(rows, 1):
            need(number <= self.policy.max_files, 'REGISTERED_RESTORE_REFERENCE_BOUND')
            yield row

    def root(self, kind, value):
        relative = registered_root(kind, value, self.policy)
        self.groups.setdefault(relative, None)
        return relative

    def rewrite(self, table, id_column, key, column, group, filename=None):
        relative = group + ('/' + filename if filename is not None else '')
        safe_relative(relative, self.policy)
        value = str(self.final / relative)
        local_absolute(value, self.policy)
        self.rewrites.append((table, id_column, key, column, value))

    def note(self, kind, key, columns, storage='registered_local'):
        need(len(self.coverage) < self.policy.max_files, 'REGISTERED_RESTORE_REFERENCE_BOUND')
        self.coverage.append({'kind': kind, 'record_sha256': record_key(kind, key),
                              'storage': storage, 'registry_columns': sorted(columns)})

    def expect(self, kind, key, role, root, relative, version=None, expected=None, storage='regular', optional=False):
        parts = safe_relative(relative, self.policy)
        name = 'registered/' + kind + '/' + record_key(kind, key) + '/' + role + '/' + '/'.join(parts)
        safe_relative(name, self.policy)
        if optional and name not in self.assets:
            return None
        need(name in self.assets, 'REGISTERED_RESTORE_MISSING_ASSET')
        item = self.assets[name]
        need(item['kind'] == kind and item['record_sha256'] == record_key(kind, key)
             and item['role'] == role and item['relative_path'] == relative and item['storage'] == storage,
             'REGISTERED_RESTORE_ASSET_SCOPE')
        if expected is not None:
            need(item['sha256'] == expected, 'REGISTERED_RESTORE_SOURCE_HASH')
        old_file = str(_source_path(root, self.policy).joinpath(*parts)).casefold()
        need(old_file not in self.managed, 'REGISTERED_RESTORE_MANAGED_CONFLICT')
        group = self.root(kind, root)
        target = group + '/' + relative
        safe_relative(target, self.policy)
        need(name not in self.claimed or self.claimed[name] == target, 'REGISTERED_RESTORE_ASSET_SCOPE')
        self.claimed[name] = target
        self.versions.setdefault(name, set()).add(digest(canonical(version)))
        if self.parent_payloads is not None:
            from .parent_restore_assets import _registered_payload_path
            return _registered_payload_path(self.parent_payloads,name,item)
        return self.payload / name

    def fingerprint(self, kind, key, role, relative, fingerprint):
        size,mtime = parse_registered_fingerprint(fingerprint)
        name = 'registered/' + kind + '/' + record_key(kind,key) + '/' + role + '/' + relative
        need(name in self.assets and name in self.claimed, 'REGISTERED_RESTORE_FINGERPRINT_WITNESS_REQUIRED')
        item = self.assets[name]
        witness = digest(canonical(registered_fingerprint_witness(role,fingerprint,item['sha256'])))
        need(size == item['bytes'] and witness in item['version_sha256s'], 'REGISTERED_RESTORE_FINGERPRINT_WITNESS_REQUIRED')
        self.versions[name].add(witness)
        need(name not in self.mtimes or self.mtimes[name] == mtime, 'REGISTERED_RESTORE_FINGERPRINT_CONFLICT')
        self.mtimes[name] = mtime

    def metadata(self, kind, key, value, version, column, table, id_column):
        p = _source_path(value, self.policy)
        group = self.root(kind, str(p.parent))
        self.rewrite(table, id_column, key, column, group, p.name)
        copied = self.expect(kind, key, 'metadata', str(p.parent), p.name, version, storage='sqlite_backup')
        _single_sqlite(copied)
        return copied

    def pending_kg(self, key, roots, version, source_kind):
        for name in self.by_record.get(('kg', record_key('kg', key)), []):
            if name in self.claimed: continue
            item = self.assets[name]
            role = item['role']; relative = item['relative_path']; parts = safe_relative(relative, self.policy)
            need(role in roots and roots[role], 'REGISTERED_RESTORE_EXTRA_ASSET')
            p = _source_path(roots[role], self.policy)
            suffix = PurePosixPath(relative).suffix
            selected = (suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp', '.heic'} if role == 'image'
                        else (suffix.lower() if isinstance(p, PureWindowsPath) else suffix)
                        == ('.txt' if role == 'transcript' else '.mp4'))
            need(selected and not (role == 'video' and source_kind == 'bilibili'
                                   and any(x.startswith('.') for x in parts)), 'REGISTERED_RESTORE_EXTRA_ASSET')
            self.expect('kg', key, role, roots[role], relative, version)

    def build(self):
        with _open_readonly(self.database) as c:
            for table, column in [('voice_input', 'local_path'), ('capture_artifact', 'local_path'), ('kg_parent_textbook_ocr_artifact', 'raw_image_path')]:
                if column in _columns(c, table):
                    for row in self.rows(_rows(c, table, [column])):
                        if type(row[column]) is str: self.managed.add(row[column].casefold())
            need(c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'REGISTERED_RESTORE_DATABASE_REQUIRED')
            for row in self.rows(_rows(c, 'curriculum_corpus', ['corpus_id', 'source_database_path', 'transcript_root_path', 'video_root_path', 'source_hash'])):
                key = row['corpus_id']; version = row['source_hash']
                self.note('curriculum', key, ['source_database_path', 'transcript_root_path', 'video_root_path'])
                for column in ['transcript_root_path', 'video_root_path']:
                    self.rewrite('curriculum_corpus', 'corpus_id', key, column, self.root('curriculum', row[column]))
                source = self.metadata('curriculum', key, row['source_database_path'], version,
                                       'source_database_path', 'curriculum_corpus', 'corpus_id')
                for r in self.rows(c.execute('SELECT transcript_relative_path,video_relative_path,local_video_available FROM curriculum_resource WHERE corpus_id=? ORDER BY row_id', (key,))):
                    need(not r['local_video_available'] or r['video_relative_path'], 'REGISTERED_RESTORE_MISSING_ASSET')
                    for column, role, base in [('transcript_relative_path', 'transcript', row['transcript_root_path']), ('video_relative_path', 'video', row['video_root_path'])]:
                        if r[column]: self.expect('curriculum', key, role, base, r[column], version)
                with _open_readonly(source) as original:
                    for r in self.rows(_rows(original, 'resources', ['resource_id'])):
                        for role, base, suffix in [('transcript', row['transcript_root_path'], '.txt'), ('video', row['video_root_path'], '.mp4')]:
                            relative = str(r['resource_id']) + suffix
                            name = 'registered/curriculum/' + record_key('curriculum', key) + '/' + role + '/' + relative
                            if name not in self.claimed:
                                self.expect('curriculum', key, role, base, relative, version, optional=True)
            for row in self.rows(_rows(c, 'kg_corpus', ['corpus_id', 'source_kind', 'transcript_root', 'video_root', 'metadata_db_path', 'version_label'])):
                key = row['corpus_id']; version = row['version_label']; root = row['transcript_root']
                extra = {name: c.execute('SELECT ' + name + ' FROM kg_corpus WHERE corpus_id=?', (key,)).fetchone()[0]
                         if name in _columns(c, 'kg_corpus') else None for name in ['pdf_root', 'image_root']}
                if root in {'bundle:' + key, 'local://family-textbook-ocr-pages'}:
                    need(not row['video_root'] and not row['metadata_db_path'] and not any(extra.values()), 'REGISTERED_RESTORE_VIRTUAL_CONFLICT')
                    for r in self.rows(c.execute('SELECT resource_id,video_relative_path FROM kg_resource WHERE corpus_id=?', (key,))):
                        need(not r['video_relative_path'] and all(not p['image_relative_path'] for p in self.pages(c, r['resource_id'])), 'REGISTERED_RESTORE_VIRTUAL_CONFLICT')
                    self.note('kg', key, ['transcript_root'], 'existing_inline_or_bundle')
                    continue
                columns = ['transcript_root']
                self.rewrite('kg_corpus', 'corpus_id', key, 'transcript_root', self.root('kg', root))
                for name in ['video_root', 'pdf_root', 'image_root']:
                    value = extra.get(name) if name in extra else row[name]
                    if value:
                        columns.append(name)
                        self.rewrite('kg_corpus', 'corpus_id', key, name, self.root('kg', value))
                if row['metadata_db_path']: columns.append('metadata_db_path')
                self.note('kg', key, columns)
                video_fp = 'video_fingerprint' if 'video_fingerprint' in _columns(c,'kg_resource') else 'NULL AS video_fingerprint'
                for r in self.rows(c.execute('SELECT resource_id,transcript_relative_path,video_relative_path,transcript_sha256,video_sha256,'+video_fp+' FROM kg_resource WHERE corpus_id=? ORDER BY resource_id', (key,))):
                    for column, role, base in [('transcript_relative_path', 'transcript', root), ('video_relative_path', 'video', row['video_root'])]:
                        if r[column]:
                            self.expect('kg', key, role, base, r[column], [version, r['transcript_sha256'], r['video_sha256']],
                                        r['transcript_sha256'] if role == 'transcript' else r['video_sha256'])
                            if role == 'video' and r['video_fingerprint'] is not None:
                                self.fingerprint('kg',key,role,r[column],r['video_fingerprint'])
                    for p in self.pages(c, r['resource_id']):
                        if p['image_relative_path']:
                            need(extra['image_root'], 'REGISTERED_RESTORE_ROOT_REQUIRED')
                            self.expect('kg', key, 'image', extra['image_root'], p['image_relative_path'], p['image_sha256'], p['image_sha256'])
                            if p['image_fingerprint'] is not None:
                                self.fingerprint('kg',key,'image',p['image_relative_path'],p['image_fingerprint'])
                if row['metadata_db_path']:
                    metadata = self.metadata('kg', key, row['metadata_db_path'], version, 'metadata_db_path', 'kg_corpus', 'corpus_id')
                    with _open_readonly(metadata) as original:
                        need(not _columns(original,'textbook_book') or 'fingerprint' in _columns(original,'textbook_book'), 'REGISTERED_RESTORE_FINGERPRINT_WITNESS_REQUIRED')
                        for book in self.rows(_rows(original, 'textbook_book', ['book_id', 'relative_pdf_path', 'fingerprint'])):
                            need(extra['pdf_root'], 'REGISTERED_RESTORE_ROOT_REQUIRED')
                            self.expect('kg', key, 'pdf', extra['pdf_root'], book['relative_pdf_path'], version)
                            self.fingerprint('kg',key,'pdf',book['relative_pdf_path'],book['fingerprint'])
                        for page in self.rows(_rows(original, 'textbook_page', ['page_id', 'ocr_text_relative_path'])):
                            if page['ocr_text_relative_path']: self.expect('kg', key, 'transcript', root, page['ocr_text_relative_path'], version)
                from .registered_fingerprint_relocation import NAME as FP_NAME,ROLE as FP_ROLE,decode_provenance
                fp_input=self.expect('kg',key,FP_ROLE,root,FP_NAME,optional=True)
                if fp_input is not None:
                    with HeldFile(fp_input.parent,fp_input.name,self.policy)as held:
                        need(held.verified_length()<=self.policy.max_manifest_bytes,'REGISTERED_RESTORE_MANIFEST_BOUND')
                        raw=b''.join(held.chunks());decode_provenance(raw,self.policy.max_manifest_bytes)
                    logical='registered/kg/'+record_key('kg',key)+'/'+FP_ROLE+'/'+FP_NAME
                    self.provenance_inputs[logical]=digest(raw)
                self.pending_kg(key, {'transcript': root, 'video': row['video_root'], 'image': extra['image_root']}, version, row['source_kind'])
            for row in self.rows(_rows(c, 'curriculum_standard_source', ['source_id', 'source_key', 'local_root_path', 'pdf_filename', 'pages_filename', 'pdf_sha256', 'text_sha256'])):
                key = row['source_id']; root = row['local_root_path']
                self.note('standard', key, ['local_root_path'])
                self.rewrite('curriculum_standard_source', 'source_id', key, 'local_root_path', self.root('standard', root))
                manifest = self.expect('standard', key, 'manifest', root, 'manifest.json')
                with HeldFile(manifest.parent, manifest.name, self.policy) as held:
                    need(held.verified_length() <= self.policy.max_manifest_bytes, 'REGISTERED_RESTORE_MANIFEST_BOUND')
                    parts = []; total = 0
                    for block in held.chunks():
                        total += len(block)
                        need(total <= self.policy.max_manifest_bytes, 'REGISTERED_RESTORE_MANIFEST_BOUND')
                        parts.append(block)
                    raw = b''.join(parts)
                need(len(raw) <= self.policy.max_manifest_bytes, 'REGISTERED_RESTORE_MANIFEST_BOUND')
                standard = _json(raw)
                need(type(standard) is dict and standard.get('format') == 'hsc_official_curriculum_sources_v1'
                     and type(standard.get('sources')) is list and len(standard['sources']) <= self.policy.max_files,
                     'REGISTERED_RESTORE_STANDARD_SCOPE')
                matches = [s for s in standard['sources'] if type(s) is dict and s.get('source_key') == row['source_key']]
                need(len(matches) == 1, 'REGISTERED_RESTORE_STANDARD_SCOPE'); selected = matches[0]
                need(selected.get('pdf_filename') == row['pdf_filename']
                     and (selected.get('ocr_pages_filename') or selected.get('pages_filename')) == row['pages_filename'],
                     'REGISTERED_RESTORE_STANDARD_SCOPE')
                version = [row['pdf_sha256'], row['text_sha256']]
                for field, role in [('pdf_filename', 'pdf'), ('pages_filename', 'ocr_pages')]:
                    if row[field]: self.expect('standard', key, role, root, row[field], version, row['pdf_sha256'] if role == 'pdf' else None)
                for field, role in [('content_filename', 'content'), ('ocr_text_filename', 'ocr_text'), ('ocr_pages_filename', 'ocr_pages')]:
                    if selected.get(field): self.expect('standard', key, role, root, selected[field], version)
                from .standard_relocation_provenance import NAME,ROLE,MAX_BYTES,decode
                incoming=self.expect('standard',key,ROLE,root,NAME,optional=True)
                if incoming is not None:
                    with HeldFile(incoming.parent,incoming.name,self.policy)as held:
                        need(held.verified_length()<=MAX_BYTES,'REGISTERED_RESTORE_MANIFEST_BOUND')
                        raw=b''.join(held.chunks());decode(raw)
                    logical='registered/standard/'+record_key('standard',key)+'/'+ROLE+'/'+NAME
                    self.provenance_inputs[logical]=digest(raw)
        self.coverage.sort(key=lambda x: (x['kind'], x['record_sha256']))
        if self.manifest['coverage'] is not None:
            need(self.coverage == self.manifest['coverage'], 'REGISTERED_RESTORE_COVERAGE_MISMATCH')
        need(set(self.assets) == set(self.claimed), 'REGISTERED_RESTORE_EXTRA_ASSET')
        for name, item in self.assets.items():
            need(item['version_sha256s'] == sorted(self.versions[name]), 'REGISTERED_RESTORE_VERSION_MISMATCH')
        if self.fingerprint_relocation is not None:
            from .registered_fingerprint_relocation import platform_precision_ns,NAME as FP_NAME
            relocation=self.fingerprint_relocation
            need(relocation['precision_ns']==platform_precision_ns(),'REGISTERED_RESTORE_TIMESTAMP_PRECISION')
            for logical,spec in relocation['metadata_changes'].items():
                need(logical in self.claimed and logical in self.derived_outputs['sources']
                    and self.assets[logical]['role']=='metadata'and self.assets[logical]['storage']=='sqlite_backup'
                    and self.assets[logical]['sha256']==spec['source_sha256']
                    and self.claimed[logical]==spec['target_relative'],'REGISTERED_RESTORE_PAYLOAD_CHANGED')
                output=self.derived_outputs['sources'][logical]
                need(output['relative']==spec['target_relative'],'REGISTERED_RESTORE_PAYLOAD_CHANGED')
                self.provenance_inputs[logical]=spec['source_sha256']
            need(set(self.derived_outputs['sources'])==set(relocation['metadata_changes']),'REGISTERED_RESTORE_PAYLOAD_CHANGED')
        targets = {}; folded = {}
        for name, target in self.claimed.items():
            if name in self.provenance_inputs:continue  # Authenticated derived input, not an overwrite.
            item = self.assets[name]; value = (item['bytes'], item['sha256'])
            need(target.casefold() not in folded or folded[target.casefold()] == target, 'REGISTERED_RESTORE_PATH_COLLISION')
            folded[target.casefold()] = target
            need(target not in targets or targets[target][1] == value, 'REGISTERED_RESTORE_PATH_COLLISION')
            targets.setdefault(target, (name, value))
        if self.fingerprint_relocation is not None:
            from .registered_fingerprint_relocation import NAME as FP_NAME
            allowed={spec['target_relative']for spec in self.fingerprint_relocation['metadata_changes'].values()}
            allowed.update(group+'/'+FP_NAME for group in self.fingerprint_relocation['corpus_groups'].values())
            for record in self.derived_outputs['records']:
                target=record['relative'];safe_relative(target,self.policy)
                need(target in allowed and target not in targets and(target.casefold()not in folded or folded[target.casefold()]==target),
                    'REGISTERED_RESTORE_PATH_COLLISION')
                folded[target.casefold()]=target;self.derived_targets.add(target)
                targets[target]=('derived/'+target,(record['bytes'],record['sha256']))
        self.targets = targets; self.target_mtimes = {}
        for name,mtime in self.mtimes.items():
            target = self.claimed[name]
            if self.fingerprint_relocation is not None:
                planned=self.fingerprint_relocation['targets'].get(target)
                need(planned is not None and planned['source_mtime_ns']==mtime,'REGISTERED_RESTORE_FINGERPRINT_CONFLICT')
                mtime=planned['target_mtime_ns']
            need(target not in self.target_mtimes or self.target_mtimes[target] == mtime, 'REGISTERED_RESTORE_FINGERPRINT_CONFLICT')
            self.target_mtimes[target] = mtime
        if self.fingerprint_relocation is not None:
            need(set(self.target_mtimes)==set(self.fingerprint_relocation['targets']),'REGISTERED_RESTORE_FINGERPRINT_CONFLICT')
        return self

    def pages(self, connection, key):
        if not _columns(connection, 'kg_document_page'): return []
        need(connection.execute("SELECT type FROM sqlite_master WHERE name='kg_document_page'").fetchone()[0] == 'table', 'REGISTERED_RESTORE_SCHEMA_REQUIRED')
        image_fp = 'image_fingerprint' if 'image_fingerprint' in _columns(connection,'kg_document_page') else 'NULL AS image_fingerprint'
        return self.rows(connection.execute('SELECT image_relative_path,image_sha256,'+image_fp+' FROM kg_document_page WHERE resource_id=? ORDER BY page_id', (key,)))


_REWRITE_COLUMNS = {
    'voice_input': {'local_path'},
    'capture_artifact': {'local_path'},
    'kg_parent_textbook_ocr_artifact': {'raw_image_path'},
    'kg_corpus': {'transcript_root','video_root','metadata_db_path','pdf_root','image_root'},
    'curriculum_corpus': {'source_database_path','transcript_root_path','video_root_path'},
    'curriculum_standard_source': {'local_root_path'},
}
_PROTECTED_PROJECTIONS = (
    'voice_input','capture_artifact','kg_parent_textbook_ocr_artifact',
    'kg_corpus','kg_resource','kg_document_page','kg_segment','kg_segment_occurrence',
    'curriculum_corpus','curriculum_resource','curriculum_standard_source','curriculum_standard_page',
    'paper_scoring_source_catalog_revision',
)


def _projection_digest(connection, rewrites=(), fingerprint_changes=(), catalog_increments=0):
    from .registered_fingerprint_relocation import main_rewrite_tuples
    fp_rewrites=main_rewrite_tuples(list(fingerprint_changes))
    changes = {};fp_permitted={(t,c)for t,_,_,c,_ in fp_rewrites}
    for table,id_column,key,column,value in list(rewrites)+fp_rewrites:
        need((table,column)in fp_permitted or(table in _REWRITE_COLUMNS and column in _REWRITE_COLUMNS[table]), 'REGISTERED_RESTORE_SQL_AUTHORIZATION_DENIED')
        by_id = changes.setdefault(table,(id_column,{}))
        need(by_id[0] == id_column,'REGISTERED_RESTORE_SCHEMA_REQUIRED')
        by_id[1].setdefault(key,{})[column] = value
    need(type(catalog_increments)is int and catalog_increments>=0,'REGISTERED_RESTORE_SQL_AUTHORIZATION_DENIED')
    if catalog_increments:
        from .registered_fingerprint_relocation import CATALOG_TABLE
        row=connection.execute('SELECT revision FROM '+CATALOG_TABLE+' WHERE singleton=1').fetchone()
        need(row is not None and type(row[0])is int and 0<=row[0]<=2**63-1-catalog_increments,'REGISTERED_RESTORE_SQL_AUTHORIZATION_DENIED')
        changes[CATALOG_TABLE]=('singleton',{1:{'revision':row[0]+catalog_increments}})
    sha = hashlib.sha256()
    # Preserve schema/triggers as data; never drop or trust them as authority.
    for row in connection.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'):
        sha.update(canonical(list(row))); sha.update(b'\n')
    for table in _PROTECTED_PROJECTIONS:
        schema = connection.execute('SELECT type,sql FROM sqlite_master WHERE name=?',(table,)).fetchone()
        if schema is None: continue
        need(schema[0] == 'table' and not schema[1].lstrip().upper().startswith('CREATE VIRTUAL'), 'REGISTERED_RESTORE_SCHEMA_REQUIRED')
        columns = [r[1] for r in connection.execute('PRAGMA table_info('+table+')')]
        need('_rowid_' not in columns, 'REGISTERED_RESTORE_SCHEMA_REQUIRED')
        sha.update(canonical([table,columns]))
        count = 0
        for row in connection.execute('SELECT * FROM '+table+' ORDER BY _rowid_'):
            values = dict(zip(columns,row))
            if table in changes:
                id_column,by_key = changes[table]
                values.update(by_key.get(values[id_column],{}))
            # Digests remain internal; no prompt, SQL text or IDs are exported.
            encoded = []
            for column in columns:
                value = values[column]
                if isinstance(value,bytes): value = ['blob_sha256',digest(value),len(value)]
                elif isinstance(value,float): value = ['float_hex',value.hex()]
                encoded.append(value)
            sha.update(canonical(encoded)); sha.update(b'\n'); count += 1
        sha.update(canonical(['count',count]))
    return sha.hexdigest()


def _rewrite_fixed_paths(connection, rewrites, fingerprint_changes=()):
    from .registered_fingerprint_relocation import main_rewrite_tuples,apply_main_fingerprint_changes,catalog_revision_contract,CATALOG_TABLE
    fingerprint_changes=list(fingerprint_changes);fp_rewrites=main_rewrite_tuples(fingerprint_changes)
    catalog=catalog_revision_contract(connection,fingerprint_changes)
    expected = _projection_digest(connection,rewrites,fingerprint_changes,catalog['increments'])
    permitted = {(table,column) for table,_,_,column,_ in rewrites}
    need(all(table in _REWRITE_COLUMNS and column in _REWRITE_COLUMNS[table] for table,column in permitted),
         'REGISTERED_RESTORE_SQL_AUTHORIZATION_DENIED')
    ids = {(table,id_column) for table,id_column,_,_,_ in rewrites}
    fp_permitted={(table,column)for table,_,_,column,_ in fp_rewrites}
    fp_reads=({('kg_resource','resource_id'),('kg_resource','corpus_id'),('kg_resource','video_fingerprint'),('kg_document_page','page_id'),('kg_document_page','resource_id'),('kg_document_page','image_fingerprint')}if fp_rewrites else set())
    denied = [False]
    def authorize(action, first, second, database, source):
        ok = source is None and (
            (action == sqlite3.SQLITE_UPDATE and database == 'main' and (first,second) in (permitted|fp_permitted))
            or (action == sqlite3.SQLITE_READ and database == 'main' and (first,second) in (permitted|ids|fp_reads))
            or(action==sqlite3.SQLITE_SELECT and bool(fp_rewrites))
            or (action == sqlite3.SQLITE_TRANSACTION and first in {'BEGIN','COMMIT','ROLLBACK'}))
        if source in catalog['origins']:
            ok=(database=='main'and first==CATALOG_TABLE and(
                (action==sqlite3.SQLITE_READ and second in {'singleton','revision'})
                or(action==sqlite3.SQLITE_UPDATE and second=='revision')))
        if not ok: denied[0] = True
        return sqlite3.SQLITE_OK if ok else sqlite3.SQLITE_DENY
    connection.set_authorizer(authorize)
    try:
        connection.execute('BEGIN IMMEDIATE')
        for table,id_column,key,column,value in rewrites:
            result = connection.execute('UPDATE '+table+' SET '+column+'=? WHERE '+id_column+'=?',(value,key))
            need(result.rowcount == 1,'REGISTERED_RESTORE_REGISTRY_CHANGED')
        apply_main_fingerprint_changes(connection,fingerprint_changes)
        connection.commit()
    except BaseException:
        connection.rollback()
        if denied[0]: raise RegisteredAssetRestoreError('REGISTERED_RESTORE_SQL_AUTHORIZATION_DENIED') from None
        raise
    # Keep the restrictive authorizer until this write connection closes.
    # Python3.10 cannot reliably disable it with None; use a new RO connection
    # for post-write checks instead of an allow-all callback fallback.
    return expected


def _verify_rewritten_projection(connection,expected,rewrites,fingerprint_changes=()):
    need(_projection_digest(connection) == expected,'REGISTERED_RESTORE_PROJECTION_CHANGED')
    from .registered_fingerprint_relocation import main_rewrite_tuples
    for table,id_column,key,column,value in list(rewrites)+main_rewrite_tuples(list(fingerprint_changes)):
        rows = connection.execute('SELECT '+column+' FROM '+table+' WHERE '+id_column+'=?',(key,)).fetchall()
        need(len(rows) == 1 and rows[0][0] == value,'REGISTERED_RESTORE_REGISTRY_CHANGED')


def _remove_owned_tree(parent, name, policy):
    """Bounded FD-relative erase on the owned target; no dir_fd rmtree fallback."""
    if os.name == 'nt':
        shutil.rmtree(io_path(parent / name))
        return
    visited = 0
    def erase(parent_fd, child_name, depth):
        nonlocal visited
        need(depth <= policy.max_depth+4, 'REGISTERED_RESTORE_CLEANUP_BOUND')
        info = os.stat(child_name, dir_fd=parent_fd, follow_symlinks=False)
        need(stat.S_ISDIR(info.st_mode), 'REGISTERED_RESTORE_TARGET_ALIAS')
        fd = os.open(child_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            held = os.fstat(fd)
            need((held.st_dev, held.st_ino) == (info.st_dev, info.st_ino), 'REGISTERED_RESTORE_TARGET_ALIAS')
            with os.scandir(io_path(fd)) as entries:
                for entry in entries:
                    visited += 1
                    need(visited <= (policy.max_files+16)*(policy.max_depth+4), 'REGISTERED_RESTORE_CLEANUP_BOUND')
                    before = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(before.st_mode): erase(fd, entry.name, depth+1)
                    else: os.unlink(entry.name, dir_fd=fd)
            current = os.stat(child_name, dir_fd=parent_fd, follow_symlinks=False)
            need((current.st_dev, current.st_ino) == (held.st_dev, held.st_ino), 'REGISTERED_RESTORE_TARGET_ALIAS')
        finally: os.close(fd)
        os.rmdir(child_name, dir_fd=parent_fd)
    erase(parent, name, 0)


class _TargetWin32(_CheckedWin32):
    """Target-only capabilities; source adapter and non-owned ancestors unchanged."""
    def open_target(self,path,*,write=False,file_attributes=False):
        access = (0x40000180 if file_attributes else (0x40000080 if write else 0x80))
        flags = 0x00200000 | (0 if file_attributes else 0x02000000)
        handle = self.dll.CreateFileW('\\\\?\\'+path,access,3,None,3,flags,None)
        if handle in {None,0,self.ct.c_void_p(-1).value}:
            raise self.ct.WinError(self.ct.get_last_error())
        return handle

    def set_mtime(self,handle,mtime):
        need(mtime % 100 == 0,'REGISTERED_RESTORE_TIMESTAMP_PRECISION')
        ticks = mtime//100 + 116444736000000000
        need(0 <= ticks < 2**64,'REGISTERED_RESTORE_TIMESTAMP_PRECISION')
        ct = self.ct
        class FileTime(ct.Structure):
            _fields_ = [('low',ct.c_uint32),('high',ct.c_uint32)]
        value = FileTime(ticks & 0xffffffff,ticks >> 32)
        fn = self.dll.SetFileTime
        fn.argtypes = [ct.c_void_p,ct.c_void_p,ct.c_void_p,ct.POINTER(FileTime)]
        fn.restype = ct.c_int
        if not fn(handle,None,None,ct.byref(value)):
            raise ct.WinError(ct.get_last_error())


def _win_target_check(kernel,handle,path,*,directory=True,identity=None):
    info = kernel.info(handle)
    need(info['file_type'] == 1 and bool(info['attributes'] & 0x10) == directory
         and not info['attributes'] & (0x400|0x1000)
         and (identity is None or info['identity'] == identity)
         and kernel.final_path(handle).removeprefix('\\\\?\\').rstrip('\\').casefold()
             == str(path).rstrip('\\').casefold(),'REGISTERED_RESTORE_TARGET_ALIAS')
    return info


class _OwnedGeneration:
    """All POSIX writes are relative to held nofollow directory FDs.

    Windows keeps original shareREAD directory chains while using canonical
    paths. Directory durability failures are fatal, without a success fallback.
    Caller owns this private target root exclusively during preparation.
    """
    def __init__(self, target, policy):
        self.target = target; self.policy = policy; self.stack = ExitStack()
        token = uuid4().hex
        self.work_name = '.registered-restore-' + token
        self.final_name = 'registered-generation-' + token
        self.work = target / self.work_name; self.final = target / self.final_name
        self.published = False; self.created = False; self.dirs = {}; self.identities = {}; self.handles = []

    def __enter__(self):
        try:
            if os.name == 'nt':
                self.kernel = _TargetWin32(); parts = _windows_parts(str(self.target))
                need(self.kernel.drive_type(parts[0]) in {2, 3}, 'REGISTERED_RESTORE_LOCAL_TARGET_REQUIRED')
                for i in range(len(parts)):
                    path = str(PureWindowsPath(*parts[:i+1])); handle = (self.kernel.open_target(path) if i == len(parts)-1 else self.kernel.open(path,directory=True))
                    self.stack.callback(self.kernel.close, handle)
                    self.handles.append((handle, path, self.kernel.info(handle)['identity']))
                self.check(); self.flush_directory(self.handles[-1][0])
                os.mkdir(io_path(self.work), 0o700); self.created = True
                self.dirs[''] = self._win_open(self.work)
            else:
                need(os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW')
                     and shutil.rmtree.avoids_symlink_attacks, 'REGISTERED_RESTORE_SAFE_TARGET_REQUIRED')
                fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY); self.stack.callback(os.close, fd)
                self.chain = []
                for part in self.target.parts[1:]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    self.stack.callback(os.close, child)
                    self.chain.append((fd, part, child, os.fstat(child))); fd = child
                self.root_fd = fd; info = os.fstat(fd)
                need(info.st_uid == os.getuid() and info.st_mode & 0o077 == 0, 'REGISTERED_RESTORE_PRIVATE_TARGET_REQUIRED')
                self.check()
                os.mkdir(self.work_name, 0o700, dir_fd=fd); self.created = True
                self.dirs[''] = os.open(self.work_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            self.identities[''] = self._dir_identity(self.dirs[''])
            return self
        except BaseException:
            try:
                try:
                    if self.created: self.cleanup()
                except Exception:
                    failure = RegisteredAssetRestoreError('REGISTERED_RESTORE_CLEANUP_UNCONFIRMED')
                    failure.owned_generation = self.work
                    raise failure from None
            finally: self.stack.close()
            raise

    def _win_open(self, path):
        handle = self.kernel.open_target(str(path))
        try:
            info = self.kernel.info(handle)
            need(info['file_type'] == 1 and info['attributes'] & 0x10
                 and not info['attributes'] & (0x400 | 0x1000)
                 and self.kernel.final_path(handle).removeprefix('\\\\?\\').rstrip('\\').casefold()
                     == str(path).rstrip('\\').casefold(), 'REGISTERED_RESTORE_TARGET_ALIAS')
            return handle
        except BaseException:
            self.kernel.close(handle)
            raise

    def check(self):
        if os.name == 'nt':
            for handle, path, identity in self.handles:
                info = self.kernel.info(handle)
                need(info['identity'] == identity and info['file_type'] == 1 and info['attributes'] & 0x10
                     and not info['attributes'] & (0x400 | 0x1000)
                     and self.kernel.final_path(handle).removeprefix('\\\\?\\').rstrip('\\').casefold()
                         == path.rstrip('\\').casefold(), 'REGISTERED_RESTORE_TARGET_ALIAS')
        else:
            for parent, name, child, before in self.chain:
                now = os.stat(name, dir_fd=parent, follow_symlinks=False); held = os.fstat(child)
                need(stat.S_ISDIR(now.st_mode) and (now.st_dev, now.st_ino) == (before.st_dev, before.st_ino)
                     and (held.st_dev, held.st_ino) == (before.st_dev, before.st_ino), 'REGISTERED_RESTORE_TARGET_ALIAS')

    def _dir_identity(self, handle):
        if os.name == 'nt': return self.kernel.info(handle)['identity']
        value = os.fstat(handle); return (value.st_dev, value.st_ino)

    def release_dirs(self):
        for key in list(self.dirs)[::-1]:
            if not key: continue
            handle = self.dirs.pop(key)
            if os.name == 'nt': self.kernel.close(handle)
            else: os.close(handle)

    def assert_dir(self, relative):
        need(self._dir_identity(self.dirs[relative]) == self.identities[relative], 'REGISTERED_RESTORE_TARGET_ALIAS')
        if os.name == 'nt':
            handle = self.dirs[relative]; path = self.work / relative
            info = self.kernel.info(handle)
            need(info['file_type'] == 1 and info['attributes'] & 0x10 and not info['attributes'] & (0x400 | 0x1000)
                 and self.kernel.final_path(handle).removeprefix('\\\\?\\').rstrip('\\').casefold()
                     == str(path).rstrip('\\').casefold(), 'REGISTERED_RESTORE_TARGET_ALIAS')
        else:
            if relative:
                parts = relative.split('/'); parent = self.dirs['/'.join(parts[:-1])]; name = parts[-1]
            else: parent = self.root_fd; name = self.final_name if self.published else self.work_name
            now = os.stat(name, dir_fd=parent, follow_symlinks=False); held = os.fstat(self.dirs[relative])
            need(stat.S_ISDIR(now.st_mode) and (now.st_dev, now.st_ino) == (held.st_dev, held.st_ino),
                 'REGISTERED_RESTORE_TARGET_ALIAS')

    def validate_namespace(self, expected):
        self.check(); found = set(); visited = 0
        for relative in self.identities:
            fd = self.mkdir(relative); self.assert_dir(relative)
            with os.scandir(io_path(self.work / relative if os.name == 'nt' else fd)) as entries:
                for entry in entries:
                    visited += 1
                    need(visited <= (self.policy.max_files+1)*(self.policy.max_depth+1), 'REGISTERED_RESTORE_REFERENCE_BOUND')
                    name = relative + '/' + entry.name if relative else entry.name
                    info = entry.stat(follow_symlinks=False)
                    need(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & (0x400 | 0x1000),
                         'REGISTERED_RESTORE_TARGET_ALIAS')
                    if stat.S_ISDIR(info.st_mode): need(name in self.identities, 'REGISTERED_RESTORE_NAMESPACE_MISMATCH')
                    else:
                        need(stat.S_ISREG(info.st_mode) and name in expected, 'REGISTERED_RESTORE_NAMESPACE_MISMATCH')
                        if os.name == 'nt':
                            # Windows DirEntry.stat caches zero nlink/dev/ino.
                            # The original nofollow held chain checks the real
                            # native identity, regular type and links == 1.
                            with HeldFile(self.work, name, self.policy) as held:
                                held.check()
                            self.assert_dir(relative)
                        else:
                            need(info.st_nlink == 1, 'REGISTERED_RESTORE_NAMESPACE_MISMATCH')
                        found.add(name)
            self.release_dirs()
        need(found == expected, 'REGISTERED_RESTORE_NAMESPACE_MISMATCH')

    def mkdir(self, relative):
        if not relative: return self.dirs['']
        safe_relative(relative, self.policy); self.check()
        current = ''; parent = self.dirs['']; self.assert_dir('')
        for part in relative.split('/'):
            current = current + '/' + part if current else part
            if current not in self.dirs:
                if os.name == 'nt':
                    if current not in self.identities: os.mkdir(io_path(self.work / current), 0o700)
                    self.dirs[current] = self._win_open(self.work / current)
                else:
                    if current not in self.identities: os.mkdir(part, 0o700, dir_fd=parent)
                    self.dirs[current] = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                self.identities.setdefault(current, self._dir_identity(self.dirs[current]))
            self.assert_dir(current); parent = self.dirs[current]
        return parent

    def space(self, needed):
        need(type(needed) is int and 0 <= needed <= 2**53-1, 'REGISTERED_RESTORE_SIZE_BOUND')
        if os.name == 'nt': free = shutil.disk_usage(io_path(self.target)).free
        else:
            v = os.fstatvfs(self.root_fd); free = v.f_bavail * v.f_frsize
        headroom = min(32*1024*1024, max(8*1024*1024, free//32))
        need(free > needed + headroom, 'REGISTERED_RESTORE_DISK_REQUIRED')

    def copy(self, held, relative, expected):
        parts = safe_relative(relative, self.policy); parent = self.mkdir('/'.join(parts[:-1]))
        before = held.hash()
        need(before == (expected['sha256'], expected['bytes']), 'REGISTERED_RESTORE_PAYLOAD_CHANGED')
        self.space(expected['bytes']); self.check()
        if os.name == 'nt':
            fd = os.open(io_path(self.work / relative), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
        else:
            fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        total = 0; sha = hashlib.sha256()
        with os.fdopen(fd, 'wb') as out:
            for block in held.chunks():
                total += len(block)
                need(total <= expected['bytes'], 'REGISTERED_RESTORE_PAYLOAD_CHANGED')
                need(out.write(block) == len(block), 'REGISTERED_RESTORE_WRITE_FAILED')
                sha.update(block)
            out.flush(); os.fsync(out.fileno())
        need(before == (sha.hexdigest(), total) == held.hash(), 'REGISTERED_RESTORE_PAYLOAD_CHANGED')
        self.check(); self.release_dirs()

    def move(self, source, target):
        a = safe_relative(source, self.policy); b = safe_relative(target, self.policy)
        dest = self.mkdir('/'.join(b[:-1])); origin = self.mkdir('/'.join(a[:-1]))
        self.check()
        if os.name == 'nt': os.rename(io_path(self.work/source), io_path(self.work/target))
        else: os.rename(a[-1], b[-1], src_dir_fd=origin, dst_dir_fd=dest)
        self.release_dirs()

    def flush_directory(self, handle):
        try:
            if os.name != 'nt':
                os.fsync(handle)
                return
            self.check()
            if handle == self.handles[-1][0]: path = self.target
            else:
                matches = [key for key,value in self.dirs.items() if value == handle]
                need(len(matches) == 1,'REGISTERED_RESTORE_TARGET_ALIAS')
                path = self.work / matches[0]
            before = _win_target_check(self.kernel,handle,path)
            temporary = self.kernel.open_target(str(path),write=True)
            try:
                _win_target_check(self.kernel,temporary,path,identity=before['identity'])
                self.kernel.flush(temporary)
                _win_target_check(self.kernel,temporary,path,identity=before['identity'])
            finally: self.kernel.close(temporary)
            _win_target_check(self.kernel,handle,path,identity=before['identity'])
            self.check()
        except OSError:
            raise RegisteredAssetRestoreError('REGISTERED_RESTORE_DIRECTORY_FLUSH_UNCONFIRMED') from None

    def flush_file(self,relative):
        parts = safe_relative(relative,self.policy); parent = self.mkdir('/'.join(parts[:-1])); self.check()
        if os.name == 'nt':
            path = self.work/relative
            handle = self.kernel.open_target(str(path),file_attributes=True)
            try:
                before = _win_target_check(self.kernel,handle,path,directory=False)
                need(before['links'] == 1,'REGISTERED_RESTORE_TARGET_ALIAS')
                self.kernel.flush(handle)
                _win_target_check(self.kernel,handle,path,directory=False,identity=before['identity'])
            finally: self.kernel.close(handle)
        else:
            fd = os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
            try: os.fsync(fd)
            finally: os.close(fd)
        self.check(); self.release_dirs()

    @contextmanager
    def derived_file(self,relative):
        """CreateNew mutable preparation file under this unpublished generation."""
        parts=safe_relative(relative,self.policy);parent=self.mkdir('/'.join(parts[:-1]));self.check()
        fd=None
        try:
            if os.name=='nt':
                fd=os.open(io_path(self.work/relative),os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_BINARY,0o600)
                import msvcrt
                handle=msvcrt.get_osfhandle(fd);identity=_win_target_check(self.kernel,handle,self.work/relative,directory=False)['identity']
                def check():
                    self.check();info=_win_target_check(self.kernel,handle,self.work/relative,directory=False,identity=identity)
                    need(info['links']==1,'REGISTERED_RESTORE_TARGET_ALIAS')
            else:
                fd=os.open(parts[-1],os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
                before=os.fstat(fd);identity=(before.st_dev,before.st_ino)
                def check():
                    self.check();at=os.stat(parts[-1],dir_fd=parent,follow_symlinks=False);held=os.fstat(fd)
                    need(stat.S_ISREG(at.st_mode)and at.st_nlink==held.st_nlink==1
                        and(at.st_dev,at.st_ino)==(held.st_dev,held.st_ino)==identity,'REGISTERED_RESTORE_TARGET_ALIAS')
            check();yield self.work/relative,fd,check
            check();os.fsync(fd)
        finally:
            if fd is not None:os.close(fd)
        self.release_dirs()

    def set_mtime(self,relative,mtime,expected):
        with HeldFile(self.work,relative,self.policy) as held:
            need(held.hash() == (expected[1],expected[0]),'REGISTERED_RESTORE_PAYLOAD_CHANGED')
        parts = safe_relative(relative,self.policy); parent = self.mkdir('/'.join(parts[:-1])); self.check()
        if os.name == 'nt':
            path = self.work/relative
            handle = self.kernel.open_target(str(path),file_attributes=True)
            try:
                info = _win_target_check(self.kernel,handle,path,directory=False)
                need(info['links'] == 1 and info['size'] == expected[0],'REGISTERED_RESTORE_PAYLOAD_CHANGED')
                self.kernel.set_mtime(handle,mtime)
                self.kernel.flush(handle)
                _win_target_check(self.kernel,handle,path,directory=False,identity=info['identity'])
            finally: self.kernel.close(handle)
        else:
            fd = os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
            try:
                before = os.fstat(fd)
                need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size == expected[0],
                     'REGISTERED_RESTORE_PAYLOAD_CHANGED')
                os.utime(fd,ns=(before.st_atime_ns,mtime)); os.fsync(fd)
                after = os.fstat(fd)
                need((before.st_dev,before.st_ino) == (after.st_dev,after.st_ino) and after.st_mtime_ns == mtime,
                     'REGISTERED_RESTORE_TIMESTAMP_PRECISION')
            finally: os.close(fd)
        self.release_dirs()
        with HeldFile(self.work,relative,self.policy) as held:
            need(held_registered_fingerprint(held) == f'size:{expected[0]}:mtime_ns:{mtime}',
                 'REGISTERED_RESTORE_TIMESTAMP_PRECISION')
            need(held.hash() == (expected[1],expected[0]),'REGISTERED_RESTORE_PAYLOAD_CHANGED')


    def flush(self):
        self.check()
        # A failed native directory flush is a HOLD, never replaced with a no-op.
        for key in sorted(self.identities, key=lambda x: len(x.split('/')), reverse=True):
            handle = self.mkdir(key); self.assert_dir(key)
            self.flush_directory(handle)
            self.release_dirs()
        self.flush_directory(self.handles[-1][0] if os.name == 'nt' else self.root_fd)

    def close_dirs(self):
        first = None
        for key in list(self.dirs)[::-1]:
            handle = self.dirs.pop(key)
            try:
                if os.name == 'nt': self.kernel.close(handle)
                else: os.close(handle)
            except Exception as error:
                if first is None: first = error
        if first is not None: raise first

    def publish(self):
        self.flush(); self.check()
        # Exclusive private caller ownership plus a fresh 128-bit name; never
        # replace any prior generation. The parent owns the wider restore lock.
        if os.name == 'nt':
            need(not os.path.lexists(io_path(self.final)), 'REGISTERED_RESTORE_GENERATION_EXISTS')
            self.close_dirs()  # Original shareREAD handles deny own rename too.
            os.rename(io_path(self.work), io_path(self.final))
        else:
            try: os.stat(self.final_name, dir_fd=self.root_fd, follow_symlinks=False)
            except FileNotFoundError: pass
            else: raise RegisteredAssetRestoreError('REGISTERED_RESTORE_GENERATION_EXISTS')
            os.rename(self.work_name, self.final_name, src_dir_fd=self.root_fd, dst_dir_fd=self.root_fd)
        self.published = True
        self.flush_directory(self.handles[-1][0] if os.name == 'nt' else self.root_fd)
        self.check()

    def cleanup(self):
        self.close_dirs()
        if self.published:
            # Durable assets can remain orphaned if the parent's DB transaction
            # never commits. Retain them; never erase a published generation.
            return
        if self.created:
            _remove_owned_tree(self.target if os.name == 'nt' else self.root_fd, self.work_name, self.policy)
            self.created = False

    def __exit__(self, kind, error, tb):
        if kind is not None and self.published and error is not None:
            error.owned_generation = self.final
        try:
            try: self.cleanup()
            except Exception:
                failure = RegisteredAssetRestoreError('REGISTERED_RESTORE_CLEANUP_UNCONFIRMED')
                failure.owned_generation = self.final if self.published else self.work
                raise failure from None
        finally: self.stack.close()


@dataclass(frozen=True)
class PreparedRegisteredAssetRestore:
    status: str
    generation: Path
    database_path: Path
    source_database_sha256: str
    manifest_sha256: str
    coverage_sha256: str
    asset_count: int
    total_bytes: int


def _single_sqlite(path):
    with io_path(path).open('rb') as f: header = f.read(100)
    need(header[:16] == b'SQLite format 3\0' and header[18:20] == b'\x01\x01', 'REGISTERED_RESTORE_SINGLE_SQLITE_REQUIRED')


@contextmanager
def _legacy_fingerprint_metadata(stage,logical,item,policy):
    from .registered_fingerprint_relocation import MAX_METADATA_BYTES
    need(item['kind']=='kg'and item['role']=='metadata'and item['storage']=='sqlite_backup'
        and 0<item['bytes']<=min(policy.max_file_bytes,MAX_METADATA_BYTES),'REGISTERED_RESTORE_MANIFEST_BOUND')
    with HeldFile(stage,logical,policy)as held:
        expected=(item['sha256'],item['bytes']);need(held.hash()==expected,'REGISTERED_RESTORE_PAYLOAD_CHANGED')
        _single_sqlite(stage/logical)
        with _open_readonly(stage/logical)as c:yield c
        need(held.hash()==expected,'REGISTERED_RESTORE_PAYLOAD_CHANGED')


def _legacy_fingerprint_provenance(stage,logical,item,policy):
    from .registered_fingerprint_relocation import NAME,ROLE,MAX_METADATA_BYTES,decode_provenance
    need(item['kind']=='kg'and item['role']==ROLE and item['relative_path']==NAME
        and item['storage']=='regular'and item['bytes']<=min(policy.max_manifest_bytes,MAX_METADATA_BYTES),'REGISTERED_RESTORE_MANIFEST_BOUND')
    with HeldFile(stage,logical,policy)as held:
        need(held.hash()==(item['sha256'],item['bytes']),'REGISTERED_RESTORE_PAYLOAD_CHANGED')
        raw=b''.join(held.chunks());decode_provenance(raw,policy.max_manifest_bytes);return raw


def _legacy_fingerprint_outputs(generation,relocation,manifest,metadata_source,incoming):
    from . import registered_fingerprint_relocation as fp
    assets={item['logical_name']:item for item in manifest['assets']};records={};sources={};inputs={};history={};folded={}
    def add(relative,sha,size,refs):
        safe_relative(relative,generation.policy)
        need(relative.casefold()not in folded or folded[relative.casefold()]==relative,'REGISTERED_RESTORE_PATH_COLLISION');folded[relative.casefold()]=relative
        if relative in records:
            old=records[relative];need((old['sha256'],old['bytes'])==(sha,size),'REGISTERED_RESTORE_PATH_COLLISION')
            old['references']=[json.loads(raw)for raw in sorted({canonical(r)for r in old['references']+refs})]
        else:records[relative]={'relative':relative,'purpose':'metadata','child_id':None,'references':refs,'sha256':sha,'bytes':size}
    def reference(key,column):return {'table':'kg_corpus','id_column':'corpus_id','id':key,'column':column,'child_id':None}
    physical={}
    for logical,spec in sorted(relocation['metadata_changes'].items()):
        item=assets[logical];target=spec['target_relative'];generation.space(item['bytes']*2)
        if target in physical:
            prior=physical[target]
            need(prior['source_sha256']==item['sha256']and prior['changes']==spec['changes'],'REGISTERED_RESTORE_PATH_COLLISION')
            sha,size,source_projection,target_projection=prior['result']
        else:
            with metadata_source(logical,item)as source:
                source_projection=fp.metadata_projection(source)
                with generation.derived_file(target)as(path,fd,check):
                    if os.name=='nt':
                        from urllib.parse import quote
                        from .backup_local_io import _windows_io_name
                        uri='file:'+quote(_windows_io_name(path),safe='')+'?mode=rw&vfs=win32-longpath'
                    else:uri=path.as_uri()+'?mode=rw'
                    c=sqlite3.connect(uri,uri=True,timeout=0)
                    try:target_projection=fp.rewrite_metadata_connection(source,c,spec['changes'])
                    finally:c.close()
                    check()
                    need(os.fstat(fd).st_size<=min(generation.policy.max_file_bytes,fp.MAX_METADATA_BYTES),'REGISTERED_RESTORE_DATABASE_BOUND')
            with HeldFile(generation.work,target,generation.policy)as held:
                sha,size=held.hash()
                with _open_readonly(generation.work/target)as c:fp.verify_metadata_connection(c,target_projection)
                need(held.hash()==(sha,size),'REGISTERED_RESTORE_PAYLOAD_CHANGED')
            _single_sqlite(generation.work/target)
            physical[target]={'source_sha256':item['sha256'],'changes':spec['changes'],'result':(sha,size,source_projection,target_projection)}
        add(target,sha,size,[reference(spec['corpus_id'],'metadata_db_path')])
        sources[logical]={'relative':target,'sha256':sha,'bytes':size};inputs[logical]=item['sha256']
        history.setdefault(spec['corpus_id'],[]).append({'logical_name':logical,'source_sha256':item['sha256'],
            'target_sha256':sha,'source_bytes':item['bytes'],'target_bytes':size,
            'source_projection_sha256':source_projection,'target_projection_sha256':target_projection})
    provenance,known=fp.provenance_outputs(relocation,incoming,history,generation.policy)
    for logical,sha in known.items():need(logical not in inputs or inputs[logical]==sha,'REGISTERED_RESTORE_PAYLOAD_CHANGED');inputs[logical]=sha
    for relative,(raw,refs)in provenance.items():
        generation.space(len(raw))
        with generation.derived_file(relative)as(path,fd,check):
            offset=0
            while offset<len(raw):
                written=os.write(fd,raw[offset:]);need(written>0,'REGISTERED_RESTORE_CLOSE_UNCONFIRMED');offset+=written
            check()
        with HeldFile(generation.work,relative,generation.policy)as held:need(held.hash()==(digest(raw),len(raw)),'REGISTERED_RESTORE_PAYLOAD_CHANGED')
        add(relative,digest(raw),len(raw),refs)
    return {'records':list(records.values()),'sources':sources,'source_inputs':inputs}


def _prepare_registered_asset_restore(database_snapshot, verified_asset_stage, target_data_dir, *,
                                     expected_database_sha256, registered_contract, policy):
    """Publish assets + a prepared isolated DB, without claiming restore success.

    registered_contract is the already authenticated original backup's registered
    summary, not a new root map. Zero assets needs no capsule/stage but still
    requires exact recomputed coverage digest. Main DB/media remain untouched.
    """
    policy.validate(); _sha(expected_database_sha256)
    contract = registered_contract
    need(type(contract) is dict and set(contract) == {'coverage', 'asset_count', 'total_bytes', 'manifest_sha256', 'coverage_sha256', 'capsule'},
         'REGISTERED_RESTORE_BINDING_REQUIRED')
    for name in ['manifest_sha256', 'coverage_sha256']: _sha(contract[name])
    need(type(contract['asset_count']) is int and 0 <= contract['asset_count'] <= policy.max_files
         and type(contract['total_bytes']) is int and 0 <= contract['total_bytes'] <= policy.max_total_bytes,
         'REGISTERED_RESTORE_SIZE_BOUND')
    if contract['asset_count']:
        capsule = contract['capsule']
        need(type(capsule) is dict and set(capsule) == {'size_bytes', 'sha256'}
             and type(capsule['size_bytes']) is int and 0 < capsule['size_bytes'] <= 2**53-1, 'REGISTERED_RESTORE_BINDING_REQUIRED')
        _sha(capsule['sha256'])
        need(contract['coverage'] == 'encrypted_registered_v1'
             and verified_asset_stage is not None, 'REGISTERED_RESTORE_BINDING_REQUIRED')
        stage = local_absolute(str(verified_asset_stage), policy)
        with HeldFile(stage, 'manifest.json', policy) as held:
            need(held.verified_length() <= policy.max_manifest_bytes, 'REGISTERED_RESTORE_MANIFEST_BOUND')
            raw = b''.join(held.chunks())
        need(digest(raw) == contract['manifest_sha256'], 'REGISTERED_RESTORE_BINDING_REQUIRED')
        manifest = _json(raw)
        receipt = verify_registered_asset_snapshot(stage, policy)
        need(receipt['manifest_sha256'] == contract['manifest_sha256'] and receipt['files'] == contract['asset_count']
             and receipt['total_bytes'] == contract['total_bytes']
             and digest(canonical(manifest['coverage'])) == contract['coverage_sha256'], 'REGISTERED_RESTORE_BINDING_REQUIRED')
    else:
        need(contract['coverage'] == 'zero_registered_payload' and contract['total_bytes'] == 0
             and contract['capsule'] is None and verified_asset_stage is None, 'REGISTERED_RESTORE_BINDING_REQUIRED')
        stage = None; manifest = {'assets': [], 'coverage': None}; raw = b''
    source = local_absolute(str(database_snapshot), policy)
    target = local_absolute(str(target_data_dir), policy)
    # Only the admitted private snapshot is opened; no old registry root is used.
    for suffix in ['-wal', '-shm', '-journal']:
        need(not os.path.lexists(io_path(Path(str(source) + suffix))), 'REGISTERED_RESTORE_SINGLE_SQLITE_REQUIRED')
    try:
        with HeldFile(source.parent, source.name, policy) as database:
            db_hash, db_bytes = database.hash()
            need(db_hash == expected_database_sha256, 'REGISTERED_RESTORE_DATABASE_BINDING')
        with _OwnedGeneration(target, policy) as generation:
            need(stage is None or (stage != generation.work and stage not in generation.work.parents
                                   and generation.work not in stage.parents), 'REGISTERED_RESTORE_STAGE_INTERSECTION')
            generation.space(db_bytes*2 + contract['total_bytes'] + len(raw))
            with HeldFile(source.parent,source.name,policy) as database:
                generation.copy(database,'database.db',{'sha256':db_hash,'bytes':db_bytes})
            prepared = generation.work/'database.db'
            generation.check(); generation.assert_dir(''); _single_sqlite(prepared)
            for item in manifest['assets']:
                with HeldFile(stage, item['logical_name'], policy) as held:
                    generation.copy(held, 'payloads/' + item['logical_name'], item)
            generation.check(); generation.assert_dir('')
            from . import registered_fingerprint_relocation as fp
            with _open_readonly(prepared)as original:
                original.row_factory=sqlite3.Row
                metadata_source=lambda name,item:_legacy_fingerprint_metadata(generation.work/'payloads',name,item,policy)
                relocation=fp.collect_plan(original,manifest,policy,fp.platform_precision_ns(),metadata_source)
                incoming=fp.incoming_plan(original,manifest,policy,
                    lambda name,item:_legacy_fingerprint_provenance(generation.work/'payloads',name,item,policy))
            derived=_legacy_fingerprint_outputs(generation,relocation,manifest,metadata_source,incoming)
            plan = _Plan(prepared, generation.work/'payloads', manifest, policy, generation.final,
                         fingerprint_relocation=relocation,derived_outputs=derived).build()
            need(digest(canonical(plan.coverage)) == contract['coverage_sha256'], 'REGISTERED_RESTORE_COVERAGE_MISMATCH')
            # Empty registered roots are first-class groups too.
            for relative in plan.groups:
                generation.mkdir(relative); generation.release_dirs()
            for relative, (name, _) in plan.targets.items():
                if relative in plan.derived_targets:continue
                generation.move('payloads/' + name, relative)
            for relative,mtime in plan.target_mtimes.items():
                generation.set_mtime(relative,mtime,plan.targets[relative][1])
            # Remove only our verified quarantine leftovers, including safe
            # identical duplicates that merged to one physical root/path.
            if manifest['assets']:
                generation.release_dirs()
                _remove_owned_tree(generation.work if os.name == 'nt' else generation.dirs[''], 'payloads', policy)
                for key in list(generation.identities):
                    if key == 'payloads' or key.startswith('payloads/'): del generation.identities[key]
            generation.space(db_bytes + sum(len(value.encode('utf-8'))*2 for _, _, _, _, value in plan.rewrites))
            generation.check(); generation.assert_dir('')
            connection = sqlite3.connect(prepared, timeout=0)
            try:
                connection.execute('PRAGMA trusted_schema=OFF'); connection.execute('PRAGMA journal_mode=DELETE')
                connection.execute('PRAGMA synchronous=FULL')
                expected_projection = _rewrite_fixed_paths(connection,plan.rewrites,relocation['main_changes'])
            finally: connection.close()
            with _open_readonly(prepared) as checked:
                _verify_rewritten_projection(checked,expected_projection,plan.rewrites,relocation['main_changes'])
                need(checked.execute('PRAGMA integrity_check').fetchone()[0] == 'ok','REGISTERED_RESTORE_DATABASE_REQUIRED')
            need(io_path(prepared).stat().st_size <= policy.max_file_bytes, 'REGISTERED_RESTORE_DATABASE_BOUND')
            _single_sqlite(prepared)
            for suffix in ['-wal', '-shm', '-journal']:
                need(not os.path.lexists(io_path(Path(str(prepared) + suffix))), 'REGISTERED_RESTORE_SINGLE_SQLITE_REQUIRED')
            generation.flush_file('database.db')
            for relative, (_, (size, sha)) in plan.targets.items():
                with HeldFile(generation.work, relative, policy) as held:
                    need(held.hash() == (sha, size), 'REGISTERED_RESTORE_PAYLOAD_CHANGED')
            generation.validate_namespace(set(plan.targets) | {'database.db'})
            with HeldFile(source.parent,source.name,policy) as database:
                need(database.hash() == (db_hash,db_bytes),'REGISTERED_RESTORE_DATABASE_BINDING')
            generation.publish()
            return PreparedRegisteredAssetRestore('REGISTERED_ASSETS_PUBLISHED_PREPARED_DB_ONLY', generation.final,
                generation.final/'database.db', db_hash, contract['manifest_sha256'], contract['coverage_sha256'],
                contract['asset_count'], contract['total_bytes'])
    except RegisteredAssetRestoreError: raise
    except Exception as error:
        failure = RegisteredAssetRestoreError('REGISTERED_RESTORE_PREPARATION_FAILED')
        if hasattr(error, 'owned_generation'): failure.owned_generation = error.owned_generation
        raise failure from None


def prepare_registered_asset_restore(database_snapshot, verified_asset_stage, target_data_dir, *,
                                     expected_database_sha256, registered_contract, policy):
    """Finite public boundary; only prepare/publish asset generation, never Main DB.

    The caller must authenticate the original contract before this call and
    hold its exclusive restore transaction until it publishes the prepared DB.
    """
    try:
        return _prepare_registered_asset_restore(database_snapshot, verified_asset_stage, target_data_dir,
            expected_database_sha256=expected_database_sha256, registered_contract=registered_contract, policy=policy)
    except RegisteredAssetRestoreError: raise
    except Exception as error:
        failure = RegisteredAssetRestoreError('REGISTERED_RESTORE_PREPARATION_FAILED')
        if hasattr(error, 'owned_generation'): failure.owned_generation = error.owned_generation
        raise failure from None
