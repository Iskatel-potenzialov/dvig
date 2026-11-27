import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from document_bundle_builder.builder import logical_digest,sha
from kb_assembler.assembler import assemble,canonical,resolve,validate_vectors

ROOT=Path(__file__).resolve().parents[1]
REAL=next((ROOT/'document_bundles/DOC-YAMZ-236M2-01').glob('build_*'))

def fixture(root,doc,engine='E',prefix='a'):
    root.mkdir();(root/'assets').mkdir();(root/'assets/f.png').write_bytes(b'synthetic fixture asset');(root/'source.pdf').write_bytes(b'synthetic fixture PDF '+doc.encode())
    con=sqlite3.connect(root/'knowledge.sqlite')
    con.executescript('''CREATE TABLE source_map__documents(document_id TEXT);
CREATE TABLE source_map__document_revisions(document_id TEXT,page_count INTEGER);
CREATE TABLE text_chunks__blocks(block_id TEXT);
CREATE TABLE text_chunks__chunks(chunk_id TEXT,block_id TEXT,raw_text TEXT,applicability_status TEXT);
CREATE TABLE table_layer__table_chunks(chunk_id TEXT,logical_id TEXT,raw_text TEXT);
CREATE TABLE table_layer__logical_rows(logical_id TEXT,scope_status TEXT,engine_ids_json TEXT,scope_conditions_json TEXT,scope_basis_json TEXT,original_values_json TEXT,source_cell_ids_json TEXT,source_row_ids_json TEXT);
CREATE TABLE table_layer__final_readiness_03_4(logical_id TEXT,future_candidate INTEGER);
CREATE TABLE passport_layer__passports(passport_id TEXT,scope_status TEXT);
CREATE TABLE passport_layer__passport_mapping(passport_id TEXT,chunk_id TEXT,eligible_candidate INTEGER,runtime INTEGER);
CREATE TABLE bundle_search_records(kind TEXT,source_id TEXT,document_id TEXT,search_text TEXT,search_sha256 TEXT,pages_json TEXT,engine_ids_json TEXT,scope_conditions_json TEXT,future_candidate INTEGER,runtime_enabled INTEGER,source_layer TEXT);
CREATE TABLE figure_layer__figures(figure_id TEXT,document_id TEXT,page_no INTEGER,engine_ids_json TEXT,scope_conditions_json TEXT,applicability_status TEXT,auto_attach INTEGER);
CREATE TABLE figure_layer__figure_regions(region_id TEXT,figure_id TEXT,page_no INTEGER,asset_path TEXT,asset_sha256 TEXT,engine_ids_json TEXT,scope_conditions_json TEXT,applicability_status TEXT,can_be_considered_for_stage06 INTEGER);
CREATE TABLE text_table_links__semantic_links(link_id TEXT,chunk_id TEXT,table_chunk_id TEXT,relation_type TEXT,semantic_status TEXT,future_use_candidate INTEGER,conditions_json TEXT,blocking_json TEXT);
CREATE TABLE text_figure_links__figure_chunk_links(link_id TEXT,chunk_id TEXT,figure_id TEXT,future_attach_candidate INTEGER,auto_attach INTEGER,runtime_active INTEGER,scope_conditions_json TEXT);
CREATE TABLE text_chunks__audited_safety_obligations(obligation_id TEXT);
''')
    con.execute('insert into source_map__documents values (?)',(doc,));con.execute('insert into source_map__document_revisions values (?,?)',(doc,30));con.execute('insert into text_chunks__blocks values (?)',(prefix+'b',))
    con.executemany('insert into text_chunks__chunks values (?,?,?,?)',[(prefix+'t',prefix+'b','text','confirmed'),(prefix+'x',prefix+'b','mixed','confirmed')]);con.execute('insert into table_layer__table_chunks values (?,?,?)',(prefix+'r',prefix+'l','table'));con.execute('insert into table_layer__logical_rows values (?,?,?,?,?,?,?,?)',(prefix+'l','confirmed',json.dumps([engine]),'[]','{}','[]','[]','[]'));con.execute('insert into table_layer__final_readiness_03_4 values (?,?)',(prefix+'l',1))
    con.execute('insert into table_layer__table_chunks values (?,?,?)',(prefix+'r2',prefix+'l2','standalone'));con.execute('insert into table_layer__logical_rows values (?,?,?,?,?,?,?,?)',(prefix+'l2','confirmed',json.dumps([engine]),'[]','{}','[]','[]','[]'));con.execute('insert into table_layer__final_readiness_03_4 values (?,?)',(prefix+'l2',1))
    con.execute('insert into passport_layer__passports values (?,?)',(prefix+'p','confirmed'));con.execute('insert into passport_layer__passport_mapping values (?,?,?,?)',(prefix+'p',prefix+'t',1,0))
    for kind,id,text,cond,candidate in [('text',prefix+'t','text','[]',1),('text',prefix+'x','mixed','["unresolved condition"]',1),('table',prefix+'r','table','[]',1),('table',prefix+'r2','standalone','[]',1),('passport',prefix+'p','passport','[]',1)]:con.execute('insert into bundle_search_records values (?,?,?,?,?,?,?,?,?,?,?)',(kind,id,doc,text,hashlib.sha256(text.encode()).hexdigest(),'[25]',json.dumps([engine]),cond,candidate,0,kind))
    con.execute('insert into figure_layer__figures values (?,?,?,?,?,?,?)',(prefix+'f',doc,25,json.dumps([engine]),'[]','confirmed',1));con.execute('insert into figure_layer__figure_regions values (?,?,?,?,?,?,?,?,?)',(prefix+'region',prefix+'f',25,'assets/f.png',sha(root/'assets/f.png'),json.dumps([engine]),'[]','confirmed',1))
    con.execute('insert into text_table_links__semantic_links values (?,?,?,?,?,?,?,?)',(prefix+'link',prefix+'t',prefix+'r','complement','semantic_confirmed',1,'{}','[]'))
    con.execute('insert into text_table_links__semantic_links values (?,?,?,?,?,?,?,?)',(prefix+'link2',prefix+'t',prefix+'r','same_content','semantic_confirmed',1,'{}','[]'))
    con.execute('insert into text_figure_links__figure_chunk_links values (?,?,?,?,?,?,?)',(prefix+'flink',prefix+'t',prefix+'f',1,1,1,'[]'));con.commit()
    tables=[{'output':x[0],'logical_sha256':logical_digest(con,x[0])} for x in con.execute("select name from sqlite_master where type='table'").fetchall()];con.close()
    identity={'document_id':doc,'input_sha256':{'source.pdf':sha(root/'source.pdf'),'prepared/assets/f.png':sha(root/'assets/f.png')}}
    manifest={'format':'document-bundle-v1.0','runtime_enabled':False,'identity':identity,'build_id':hashlib.sha256(canonical(identity)).hexdigest(),'source_pdf_sha256':sha(root/'source.pdf'),'logical_tables':tables}
    (root/'manifest.json').write_bytes(canonical(manifest));(root/'validation_report.json').write_bytes(canonical({'status':'passed','runtime_enabled':False}));return root

class AssemblerTests(unittest.TestCase):
 def test_real_one_document(self):
  with tempfile.TemporaryDirectory() as temp:
   path=assemble('YAMZ_236M2',[REAL],temp);m=json.loads((path/'kb_manifest.json').read_text());self.assertFalse(m['runtime_enabled']);x=json.loads((path/'search/search_inputs.json').read_text(encoding='utf8'));self.assertTrue(all(i['document_id']=='DOC-YAMZ-236M2-01' for rows in x['channels'].values() for i in rows));self.assertEqual(x['channels']['table_dense'],[])
 def test_two_documents_isolation_resolver_and_standalone(self):
  with tempfile.TemporaryDirectory() as tmp:
   t=Path(tmp);a=fixture(t/'a','doc-a',prefix='a');b=fixture(t/'b','doc-b',prefix='b');out=t/'out';first=assemble('E',[a],out);both=assemble('E',[a,b],out);self.assertNotEqual(first,both);self.assertTrue(first.exists());s=json.loads((both/'search/search_inputs.json').read_text(encoding='utf8'));self.assertEqual(len(s['channels']['table_dense']),4);self.assertIn('ar2',[x['id'] for x in s['channels']['table_dense']]);self.assertEqual(len(s['channels']['passport_dense']),2);self.assertEqual(len(s['channels']['text_bm25']),2);self.assertEqual(resolve(both,'E','text','at')['pdf_pages'],[25]);self.assertNotEqual(resolve(both,'E','text','at')['document_id'],resolve(both,'E','text','bt')['document_id']);self.assertTrue(resolve(both,'E','figure','af')['asset_path'].endswith('f.png'));self.assertRaises(ValueError,resolve,both,'OTHER','text','at')
   db=sqlite3.connect(both/'knowledge.sqlite')
   try:self.assertEqual(db.execute("select count(*) from objects where source_id='ax' and allowed=0").fetchone()[0],1);self.assertEqual(db.execute("select count(*) from relationships where kind='text_table' and relation='complement' and allowed=1").fetchone()[0],2);self.assertEqual(db.execute("select count(*) from relationships where kind='text_table' and relation='same_content' and allowed=1").fetchone()[0],2)
   finally:db.close()
   other=assemble('OTHER',[a],out);foreign=json.loads((other/'search/search_inputs.json').read_text(encoding='utf8'));self.assertTrue(all(not values for values in foreign['channels'].values()))
   self.assertEqual(assemble('E',[b,a],out),both)
 def test_bad_inputs_no_publication(self):
  with tempfile.TemporaryDirectory() as tmp:
   t=Path(tmp);a=fixture(t/'a','doc-a',prefix='a');b=fixture(t/'b','doc-b',prefix='a');out=t/'out'
   with self.assertRaisesRegex(ValueError,'colliding'):assemble('E',[a,b],out)
   self.assertFalse(any(out.rglob('kb_manifest.json')))
   (a/'source.pdf').write_bytes(b'corrupt')
   with self.assertRaisesRegex(ValueError,'hash'):assemble('E',[a],out)
 def test_vectors_fail_closed(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'v.json';p.write_text('[]');self.assertTrue(validate_vectors([],p,'text-embedding-3-large',3072,'verbatim-search-text-utf8-v1'))
   with self.assertRaises(ValueError):validate_vectors([],p,'other',3072,'verbatim-search-text-utf8-v1')
   p.write_text(json.dumps([{'id':'x','vector':[1],'search_sha256':'a','model':'text-embedding-3-large','preprocessing_version':'verbatim-search-text-utf8-v1'}]))
   with self.assertRaises(ValueError):validate_vectors([{'id':'x','search_sha256':'a'}],p,'text-embedding-3-large',3072,'verbatim-search-text-utf8-v1')
 def test_invalid_link_rejected_after_bundle_integrity_check(self):
  with tempfile.TemporaryDirectory() as tmp:
   t=Path(tmp);a=fixture(t/'a','doc-a',prefix='a');db=sqlite3.connect(a/'knowledge.sqlite');db.execute("update text_table_links__semantic_links set table_chunk_id='foreign'");db.commit();new=logical_digest(db,'text_table_links__semantic_links');db.close()
   mf=json.loads((a/'manifest.json').read_text(encoding='utf8'))
   for table in mf['logical_tables']:
    if table['output']=='text_table_links__semantic_links':table['logical_sha256']=new
   (a/'manifest.json').write_bytes(canonical(mf));out=t/'out'
   with self.assertRaisesRegex(ValueError,'invalid link endpoints'):assemble('E',[a],out)
   self.assertFalse(any(out.rglob('kb_manifest.json')))

if __name__=='__main__':unittest.main()
