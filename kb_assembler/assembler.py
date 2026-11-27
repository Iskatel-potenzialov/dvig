"""Build a preliminary engine-scoped KB; no model calls or runtime activation."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from document_bundle_builder.builder import logical_digest, sha

VERSION = 'engine-kb-assembler-v1.0'
POLICY = 'confirmed-single-engine-no-unresolved-conditions-v1'
MODEL = 'text-embedding-3-large'
PREPROCESS = 'verbatim-search-text-utf8-v1'

def canonical(x): return (json.dumps(x,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf8')
def ro(p): return sqlite3.connect(Path(p).resolve(strict=True).as_uri()+'?mode=ro',uri=True)
def parse(v):
    try:return json.loads(v) if isinstance(v,str) else v
    except (ValueError,TypeError):return None
def safe_id(v):
    if not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*',v) or v in ('.','..'):raise ValueError('invalid identifier')
    return v
def safe_file(root, rel):
    path=Path(rel)
    if path.is_absolute() or '..' in path.parts:raise ValueError('unsafe asset path')
    p=(root/path).resolve(strict=True)
    if not p.is_relative_to(root.resolve()) or not p.is_file():raise ValueError('asset outside bundle')
    return p
def allowed_scope(engine, ids, conditions, status, candidate):
    """Conservative policy: never infer that unverified conditions are satisfied."""
    engines,cond=parse(ids),parse(conditions)
    if candidate != 1:return False,'source_not_future_candidate'
    if not isinstance(engines,list) or engines != [engine]:return False,'engine_scope_not_exclusive'
    if status != 'confirmed':return False,'scope_not_confirmed'
    if cond not in ([],{}):return False,'unresolved_conditions'
    return True,'confirmed_unconditional'

def validate_bundle(path):
    path=Path(path).resolve(strict=True)
    manifest=json.loads((path/'manifest.json').read_text(encoding='utf8'))
    if manifest.get('format')!='document-bundle-v1.0' or manifest.get('runtime_enabled') is not False:raise ValueError('unsupported bundle manifest')
    ident=manifest['identity'];doc=safe_id(ident['document_id'])
    if hashlib.sha256(canonical(ident)).hexdigest()!=manifest['build_id']:raise ValueError('bundle identity mismatch')
    validation=json.loads((path/'validation_report.json').read_text(encoding='utf8'))
    if validation.get('status') not in ('passed','passed_with_warnings') or validation.get('runtime_enabled') is not False:raise ValueError('bundle validation status')
    if sha(path/'source.pdf')!=manifest['source_pdf_sha256'] or sha(path/'source.pdf')!=ident['input_sha256']['source.pdf']:raise ValueError('bundle PDF hash mismatch')
    for original,h in ident['input_sha256'].items():
        if original.startswith(('prepared/assets/','prepared/metadata/','prepared/evaluation/')):
            dest=original[len('prepared/'):]
            if sha(safe_file(path,dest))!=h:raise ValueError('bundle asset hash mismatch: '+dest)
    db=ro(path/'knowledge.sqlite');db.row_factory=sqlite3.Row
    try:
        if db.execute('pragma integrity_check').fetchall()[0][0]!='ok':raise ValueError('bundle sqlite integrity')
        for table in manifest['logical_tables']:
            if logical_digest(db,table['output'])!=table['logical_sha256']:raise ValueError('bundle table hash mismatch')
        if db.execute('select document_id from source_map__documents').fetchall()[0][0]!=doc:raise ValueError('document mismatch')
        return {'path':path,'manifest':manifest,'doc':doc,'manifest_sha':sha(path/'manifest.json')}
    finally:db.close()

def _rowdict(row): return dict(row)
def assemble(engine_id,bundles,output_root,*,rules_version=POLICY,expected_vector_model=MODEL,vector_dimensions=3072,preprocessing_version=PREPROCESS):
    engine_id=safe_id(engine_id)
    if rules_version!=POLICY or expected_vector_model!=MODEL or vector_dimensions!=3072 or preprocessing_version!=PREPROCESS:raise ValueError('unsupported policy/vector contract')
    if not bundles:raise ValueError('no input bundles')
    docs=[validate_bundle(x) for x in bundles]
    docs.sort(key=lambda x:x['doc'])
    if len({x['doc'] for x in docs})!=len(docs):raise ValueError('duplicate document ID')
    identity={'assembler_version':VERSION,'assembler_sha256':sha(__file__),'engine_id':engine_id,'policy':rules_version,'embedding_model':expected_vector_model,'embedding_dimensions':vector_dimensions,'preprocessing_version':preprocessing_version,'bundles':[{'document_id':d['doc'],'build_id':d['manifest']['build_id'],'manifest_sha256':d['manifest_sha']} for d in docs]}
    build_id=hashlib.sha256(canonical(identity)).hexdigest()
    base=Path(output_root).resolve()/('KB_'+engine_id);base.mkdir(parents=True,exist_ok=True);dest=base/build_id
    with tempfile.TemporaryDirectory(prefix='.kb-staging-',dir=base) as tmp:
        stage=Path(tmp)/'kb';stage.mkdir();(stage/'sources').mkdir();(stage/'search').mkdir()
        db=sqlite3.connect(stage/'knowledge.sqlite')
        db.executescript('''CREATE TABLE documents(document_id TEXT PRIMARY KEY,bundle_id TEXT,manifest_sha256 TEXT,pdf_path TEXT,pdf_sha256 TEXT,page_count INTEGER);
CREATE TABLE objects(kind TEXT,source_id TEXT,document_id TEXT,raw_text TEXT,search_text TEXT,search_sha256 TEXT,pages_json TEXT,engine_ids_json TEXT,conditions_json TEXT,source_status TEXT,future_candidate INTEGER,runtime_enabled INTEGER,allowed INTEGER,exclusion_reason TEXT,source_json TEXT,PRIMARY KEY(kind,source_id));
CREATE TABLE relationships(kind TEXT,source_id TEXT,document_id TEXT,left_id TEXT,right_id TEXT,relation TEXT,allowed INTEGER,exclusion_reason TEXT,source_json TEXT,PRIMARY KEY(kind,source_id));
CREATE TABLE figures(figure_id TEXT PRIMARY KEY,document_id TEXT,page INTEGER,allowed INTEGER,reason TEXT,source_json TEXT);
CREATE TABLE regions(region_id TEXT PRIMARY KEY,figure_id TEXT,document_id TEXT,page INTEGER,asset_path TEXT,asset_sha256 TEXT,allowed INTEGER,reason TEXT,source_json TEXT);
CREATE TABLE policies(source_id TEXT PRIMARY KEY,document_id TEXT,allowed INTEGER,reason TEXT,source_json TEXT);
''')
        seen={'text':set(),'table':set(),'passport':set(),'figure':set(),'region':set(),'link':set(),'policy':set(),'block':set()}
        projections={'text_bm25':[],'text_dense':[],'passport_dense':[],'table_dense':[]};excluded={};mappings=[]
        try:
            for doc in docs:
                root=doc['path'];did=doc['doc'];source=ro(root/'knowledge.sqlite');source.row_factory=sqlite3.Row
                try:
                    dr=source.execute('select * from source_map__document_revisions where document_id=?',(did,)).fetchone()
                    if dr is None or not isinstance(dr['page_count'],int) or dr['page_count']<1:raise ValueError('invalid document page count')
                    page_count=dr['page_count'];srcdir=stage/'sources'/did;srcdir.mkdir()
                    shutil.copyfile(root/'source.pdf',srcdir/'source.pdf')
                    if sha(srcdir/'source.pdf')!=doc['manifest']['source_pdf_sha256']:raise ValueError('copy PDF failed')
                    shutil.copyfile(root/'knowledge.sqlite',srcdir/'document_bundle.sqlite')
                    db.execute('insert into documents values (?,?,?,?,?,?)',(did,doc['manifest']['build_id'],doc['manifest_sha'],'sources/'+did+'/source.pdf',doc['manifest']['source_pdf_sha256'],page_count))
                    blocks={x['block_id']:_rowdict(x) for x in source.execute('select * from text_chunks__blocks')}
                    for bid in blocks:
                        if bid in seen['block']:raise ValueError('colliding block ID')
                        seen['block'].add(bid)
                    statuses={}
                    texts={x['chunk_id']:_rowdict(x) for x in source.execute('select * from text_chunks__chunks')}
                    tablerows={x['chunk_id']:_rowdict(x) for x in source.execute('select t.*,l.scope_status,l.engine_ids_json,l.scope_conditions_json,l.scope_basis_json,l.original_values_json,l.source_cell_ids_json,l.source_row_ids_json,r.future_candidate from table_layer__table_chunks t join table_layer__logical_rows l on l.logical_id=t.logical_id join table_layer__final_readiness_03_4 r on r.logical_id=l.logical_id')}
                    passports={x['passport_id']:_rowdict(x) for x in source.execute('select * from passport_layer__passports')}
                    pm=collections_map(source.execute('select passport_id,chunk_id,eligible_candidate,runtime from passport_layer__passport_mapping'))
                    search_rows=[dict(x) for x in source.execute('select * from bundle_search_records order by kind,source_id')]
                    for kind,records in [('text',texts),('table',tablerows),('passport',passports)]:
                        projected=[x['source_id'] for x in search_rows if x['kind']==kind]
                        if len(projected)!=len(set(projected)) or set(projected)!=set(records):raise ValueError('incomplete search projection: '+kind)
                    if any(x['kind'] not in ('text','table','passport') for x in search_rows):raise ValueError('unknown search kind')
                    for kind,records in [('text',texts),('table',tablerows),('passport',passports)]:
                        for oid in records:
                            if oid in seen[kind]:raise ValueError('colliding '+kind+' ID')
                            if kind in ('text','table') and (oid in seen['table' if kind=='text' else 'text']):raise ValueError('ambiguous candidate ID')
                            seen[kind].add(oid)
                    for x in search_rows:
                        kind=x['kind'];oid=x['source_id'];record={'text':texts,'table':tablerows,'passport':passports}[kind].get(oid)
                        if not record or x['document_id']!=did:raise ValueError('unknown or foreign search ID')
                        pages=parse(x['pages_json']);
                        if not isinstance(pages,list) or not pages or any(type(p)!=int or p<1 or p>page_count for p in pages):raise ValueError('invalid source pages')
                        text=x['search_text'];digest=hashlib.sha256(text.encode('utf8')).hexdigest() if isinstance(text,str) else None
                        if digest!=x['search_sha256']:raise ValueError('search text hash mismatch')
                        status=record.get('applicability_status') if kind=='text' else record.get('scope_status')
                        ok,reason=allowed_scope(engine_id,x['engine_ids_json'],x['scope_conditions_json'],status,x['future_candidate'])
                        if kind=='text' and record['block_id'] not in blocks:raise ValueError('missing parent block')
                        if not text:ok,reason=False,'missing_search_text'
                        statuses[(kind,oid)]=(ok,reason)
                        raw=record.get('raw_text') if kind!='passport' else None
                        db.execute('insert into objects values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(kind,oid,did,raw,text,digest,x['pages_json'],x['engine_ids_json'],x['scope_conditions_json'],status,x['future_candidate'],x['runtime_enabled'],int(ok),reason,json.dumps(record,ensure_ascii=False,sort_keys=True)))
                    for pid,entries in pm.items():
                        if pid not in passports or any(cid not in texts for cid,_,_ in entries):raise ValueError('invalid passport mapping')
                        if statuses.get(('passport',pid),(False,''))[0] and any(not statuses.get(('text',cid),(False,''))[0] or flag!=1 for cid,flag,_ in entries):
                            statuses[('passport',pid)]=(False,'passport_target_not_allowed')
                            db.execute('update objects set allowed=0,exclusion_reason=? where kind=? and source_id=?',('passport_target_not_allowed','passport',pid))
                        for cid,flag,runtime in entries:mappings.append({'document_id':did,'passport_id':pid,'chunk_id':cid,'source_eligible':flag,'source_runtime':runtime,'allowed':bool(statuses.get(('passport',pid),(False,''))[0] and statuses.get(('text',cid),(False,''))[0] and flag==1)})
                    for kind,records in [('text',texts),('table',tablerows),('passport',passports)]:
                        for oid in records:
                            ok,reason=statuses.get((kind,oid),(False,'missing_search_projection'))
                            if kind=='passport' and not pm.get(oid):ok,reason=False,'passport_without_mapping';db.execute('update objects set allowed=0,exclusion_reason=? where kind=? and source_id=?',(reason,kind,oid))
                            if ok:
                                row=db.execute('select search_text,search_sha256,pages_json,conditions_json from objects where kind=? and source_id=?',(kind,oid)).fetchone()
                                item={'id':oid,'document_id':did,'search_text':row[0],'search_sha256':row[1],'pdf_pages':parse(row[2]),'conditions':parse(row[3])}
                                for lane in ({'text':['text_bm25','text_dense'],'table':['table_dense'],'passport':['passport_dense']}[kind]):projections[lane].append(item)
                            else:excluded[reason]=excluded.get(reason,0)+1
                    figs={x['figure_id']:_rowdict(x) for x in source.execute('select * from figure_layer__figures')}
                    for fid,f in figs.items():
                        if fid in seen['figure']:raise ValueError('colliding figure ID')
                        seen['figure'].add(fid)
                        if f['document_id']!=did or not 1<=f['page_no']<=page_count:raise ValueError('invalid figure source')
                        ok,reason=allowed_scope(engine_id,f['engine_ids_json'],f['scope_conditions_json'],f['applicability_status'],int(f['auto_attach']==1))
                        db.execute('insert into figures values (?,?,?,?,?,?)',(fid,did,f['page_no'],int(ok),reason,json.dumps(f,ensure_ascii=False,sort_keys=True)))
                    for row in source.execute('select * from figure_layer__figure_regions'):
                        v=dict(row);rid=v['region_id'];fid=v['figure_id']
                        if rid in seen['region'] or fid not in figs:raise ValueError('invalid figure region')
                        seen['region'].add(rid)
                        if not 1<=v['page_no']<=page_count:raise ValueError('invalid figure page')
                        asset=safe_file(root,v['asset_path']);
                        if sha(asset)!=v['asset_sha256']:raise ValueError('figure PNG hash mismatch')
                        target=srcdir/Path(v['asset_path']);target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(asset,target)
                        ok,reason=allowed_scope(engine_id,v['engine_ids_json'],v['scope_conditions_json'],v['applicability_status'],int(v['can_be_considered_for_stage06']==1))
                        if not db.execute('select allowed from figures where figure_id=?',(fid,)).fetchone()[0]:ok,reason=False,'figure_not_allowed'
                        db.execute('insert into regions values (?,?,?,?,?,?,?,?,?)',(rid,fid,did,v['page_no'],'sources/'+did+'/'+v['asset_path'].replace('\\','/'),v['asset_sha256'],int(ok),reason,json.dumps(v,ensure_ascii=False,sort_keys=True)))
                    for lane,table,left,right in [('text_table','text_table_links__semantic_links','chunk_id','table_chunk_id'),('text_figure','text_figure_links__figure_chunk_links','chunk_id','figure_id')]:
                        for row in source.execute('select * from '+table):
                            v=dict(row);lid=v['link_id'];l=v[left];r=v[right]
                            if lid in seen['link']:raise ValueError('colliding link ID')
                            seen['link'].add(lid)
                            if l not in texts or (r not in tablerows if lane=='text_table' else r not in figs):raise ValueError('invalid link endpoints')
                            if lane=='text_table':
                                relation=v['relation_type']
                                if relation not in ('complement','same_content'):raise ValueError('unknown table relation')
                                cond=parse(v['conditions_json']); blocked=parse(v['blocking_json'])
                                ok=(v['semantic_status']=='semantic_confirmed' and v['future_use_candidate']==1 and
                                    cond in ([],{}) and blocked in ([],{}) and statuses.get(('text',l),(False,''))[0] and statuses.get(('table',r),(False,''))[0])
                            else:
                                ok=(v['future_attach_candidate']==1 and v['auto_attach']==1 and v['runtime_active']==1 and
                                    parse(v['scope_conditions_json']) in ([],{}) and statuses.get(('text',l),(False,''))[0] and
                                    bool(db.execute('select allowed from figures where figure_id=?',(r,)).fetchone()[0]))
                            reason='confirmed_unconditional' if ok else 'source_runtime_disabled_or_conditions_unverified'
                            db.execute('insert into relationships values (?,?,?,?,?,?,?,?,?)',(lane,lid,did,l,r,v.get('relation_type','figure'),int(ok),reason,json.dumps(v,ensure_ascii=False,sort_keys=True)))
                    for row in source.execute('select * from text_chunks__audited_safety_obligations'):
                        v=dict(row);pid=v['obligation_id']
                        if pid in seen['policy']:raise ValueError('colliding policy ID')
                        seen['policy'].add(pid)
                        db.execute('insert into policies values (?,?,?,?,?)',(pid,did,0,'policy_requires_source_scope_validation',json.dumps(v,ensure_ascii=False,sort_keys=True)))
                finally:source.close()
            db.commit()
            if db.execute('pragma integrity_check').fetchone()[0]!='ok':raise ValueError('output SQLite failed integrity')
        finally:db.close()
        for lane in projections:projections[lane].sort(key=lambda x:(x['document_id'],x['id']))
        payload={'channels':projections,'passport_mappings':mappings,'policy':POLICY,'runtime_enabled':False}
        (stage/'search/search_inputs.json').write_bytes(canonical(payload))
        index={'text_bm25':{'status':'not_built'},'text_dense':{'status':'missing_vectors'},'passport_dense':{'status':'missing_vectors'},'table_dense':{'status':'missing_vectors'},'model':MODEL,'dimensions':3072,'preprocessing_version':PREPROCESS,'vectors_generated':False}
        (stage/'search/index_manifest.json').write_bytes(canonical(index))
        verification=ro(stage/'knowledge.sqlite')
        try:logical={t:logical_digest(verification,t) for t in ('documents','objects','relationships','figures','regions','policies')}
        finally:verification.close()
        resources={f.relative_to(stage).as_posix():sha(f) for f in sorted((stage/'sources').rglob('*')) if f.is_file()}
        manifest={'format':VERSION,'build_id':build_id,'engine_id':engine_id,'identity':identity,'runtime_enabled':False,'search_inputs_sha256':sha(stage/'search/search_inputs.json'),'index_manifest_sha256':sha(stage/'search/index_manifest.json'),'knowledge_logical_sha256':logical,'resource_sha256':resources}
        report={'status':'preliminary_runtime_disabled','counts':{k:len(v) for k,v in projections.items()},'excluded_by_reason':excluded,'documents':len(docs),'vectors':'missing','conditions_policy':'fail_closed','warnings':['All conditional objects are excluded pending explicit verification.','Source runtime flags remain unchanged; output is not connected to RAG.']}
        (stage/'kb_manifest.json').write_bytes(canonical(manifest));(stage/'validation_report.json').write_bytes(canonical(report))
        if dest.exists():
            if (dest/'kb_manifest.json').read_bytes()!=(stage/'kb_manifest.json').read_bytes() or (dest/'validation_report.json').read_bytes()!=(stage/'validation_report.json').read_bytes() or sha(dest/'search/search_inputs.json')!=manifest['search_inputs_sha256']:raise ValueError('existing build mismatch')
            for file in (stage/'sources').rglob('*'):
                if file.is_file() and sha(safe_file(dest,file.relative_to(stage).as_posix()))!=sha(file):raise ValueError('existing source mismatch')
            old=ro(dest/'knowledge.sqlite');new=ro(stage/'knowledge.sqlite')
            try:
                for t in ('documents','objects','relationships','figures','regions','policies'):
                    if logical_digest(old,t)!=logical_digest(new,t):raise ValueError('existing database mismatch')
            finally:old.close();new.close()
        else:os.rename(stage,dest)
    return dest

def collections_map(rows):
    out={}
    for pid,cid,flag,runtime in rows:out.setdefault(pid,[]).append((cid,flag,runtime))
    return out

def validate_vectors(records,vector_file,model,dimensions,preprocessing_version):
    if model!=MODEL or dimensions!=3072 or preprocessing_version!=PREPROCESS:raise ValueError('vector contract mismatch')
    import math
    byid={x['id']:x for x in records};vectors=json.loads(Path(vector_file).read_text(encoding='utf8'))
    if not isinstance(vectors,list) or len(vectors)!=len(byid):raise ValueError('vector count mismatch')
    seen=set()
    for v in vectors:
        oid=v['id'];vec=v['vector']
        if oid in seen or oid not in byid or v['search_sha256']!=byid[oid]['search_sha256'] or v['model']!=model or v['preprocessing_version']!=preprocessing_version or not isinstance(vec,list) or len(vec)!=dimensions or any(type(n) not in (int,float) or not math.isfinite(n) for n in vec) or not math.isclose(math.sqrt(sum(n*n for n in vec)),1,abs_tol=1e-4):raise ValueError('invalid or incompatible vector')
        seen.add(oid)
    return True

def resolve(kb_root,engine_id,kind,source_id):
    root=Path(kb_root).resolve(strict=True);manifest=json.loads((root/'kb_manifest.json').read_text(encoding='utf8'))
    if manifest['engine_id']!=engine_id:raise ValueError('engine mismatch')
    db=ro(root/'knowledge.sqlite');db.row_factory=sqlite3.Row
    try:
        if kind=='figure':row=db.execute('select document_id,page,asset_path,asset_sha256 from regions where figure_id=? and allowed=1 order by region_id limit 1',(source_id,)).fetchone()
        else:row=db.execute('select document_id,pages_json from objects where kind=? and source_id=? and allowed=1',(kind,source_id)).fetchone()
        if not row:raise ValueError('source not allowed')
        doc=db.execute('select pdf_path,pdf_sha256 from documents where document_id=?',(row['document_id'],)).fetchone()
        pdf=safe_file(root,doc['pdf_path']);
        if sha(pdf)!=doc['pdf_sha256']:raise ValueError('PDF hash mismatch')
        result={'document_id':row['document_id'],'pdf_path':str(pdf),'pdf_pages':[row['page']] if kind=='figure' else parse(row['pages_json'])}
        if kind=='figure':
            asset=safe_file(root,row['asset_path'])
            if sha(asset)!=row['asset_sha256']:raise ValueError('PNG hash mismatch')
            result['asset_path']=str(asset)
        return result
    finally:db.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',type=Path,required=True);args=parser.parse_args();cfg=json.loads(args.config.read_text(encoding='utf8'))
    print(assemble(cfg['engine_id'],cfg['bundles'],cfg['output'],rules_version=cfg.get('rules_version',POLICY)))
if __name__=='__main__':main()
