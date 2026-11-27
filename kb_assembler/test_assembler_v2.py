import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from document_bundle_builder.builder import logical_digest,sha
from kb_assembler.assembler_v2 import assemble,resolve,filtered_digest
from kb_assembler.test_assembler import fixture, REAL

class V2Tests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name);cls.real=assemble('YAMZ_236M2',[REAL],cls.root/'real')
 @classmethod
 def tearDownClass(cls):cls.tmp.cleanup()
 def test_real_complete_rows_conditions_and_statuses(self):
  report=json.loads((self.real/'validation_report.json').read_text(encoding='utf8'))
  self.assertEqual(report['counts'],{'blocks':138,'text_chunks':148,'table_chunks':406,'passports':154,'figures':65,'regions':69,'text_table_links':236,'text_figure_links':84})
  source=sqlite3.connect(REAL/'knowledge.sqlite');out=sqlite3.connect(self.real/'knowledge.sqlite')
  try:
   tables=[r[0] for r in source.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
   for name in tables:self.assertEqual(logical_digest(source,name),filtered_digest(out,name,'DOC-YAMZ-236M2-01'))
   self.assertEqual(out.execute('select future_candidate,count(*) from table_layer__final_readiness_03_4 group by 1').fetchall(),[(0,4),(1,402)])
   self.assertEqual(out.execute('select count(*) from table_layer__table_chunks t join table_layer__final_readiness_03_4 r on t.logical_id=r.logical_id and t.bundle_document_id=r.bundle_document_id where r.future_candidate=1 and not exists (select 1 from text_table_links__semantic_links l where l.table_chunk_id=t.chunk_id and l.bundle_document_id=t.bundle_document_id)').fetchone()[0],185)
  finally:source.close();out.close()
  data=json.loads((self.real/'search/search_inputs.json').read_text(encoding='utf8'))
  self.assertEqual({k:len(v) for k,v in data['channels'].items()},{'text_bm25':148,'text_dense':148,'passport_dense':154,'table_dense':406})
  self.assertEqual(sum(x['future_candidate']==1 for x in data['channels']['table_dense']),402)
  self.assertEqual(sum(x['future_candidate']==0 for x in data['channels']['table_dense']),4)
  self.assertTrue(all(x['runtime_enabled']==0 for x in data['channels']['table_dense']))
  self.assertFalse(data['runtime_enabled'])
 def test_repeat_deterministic(self):
  self.assertEqual(assemble('YAMZ_236M2',[REAL],self.root/'real'),self.real)
 def test_two_documents_overlapping_ids_and_engine_isolation(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);a=fixture(p/'a','doc-a',prefix='a');b=fixture(p/'b','doc-b',prefix='a');output=p/'out'
   before=(sha(a/'knowledge.sqlite'),sha(b/'knowledge.sqlite'));first=assemble('E',[a],output);both=assemble('E',[a,b],output);self.assertNotEqual(first,both);self.assertTrue(first.exists());self.assertEqual(assemble('E',[b,a],output),both);self.assertEqual(before,(sha(a/'knowledge.sqlite'),sha(b/'knowledge.sqlite')))
   data=json.loads((both/'search/search_inputs.json').read_text(encoding='utf8'));self.assertEqual(len(data['channels']['table_dense']),4);self.assertEqual(len(data['channels']['text_bm25']),4)
   one=resolve(both,'E','doc-a','text','at');two=resolve(both,'E','doc-b','text','at');self.assertEqual(one['pdf_pages'],[25]);self.assertNotEqual(one['pdf_path'],two['pdf_path']);self.assertIn('doc-a',resolve(both,'E','doc-a','figure','af')['regions'][0]['asset_path']);self.assertRaises(ValueError,resolve,both,'E','doc-b','figure','missing')
   other=assemble('OTHER',[a],output);self.assertNotEqual(first,other);self.assertTrue(first.exists())
 def test_corrupt_bundle_no_partial_publication(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);a=fixture(p/'a','doc-a');(a/'source.pdf').write_bytes(b'corrupt');output=p/'out'
   with self.assertRaises(ValueError):assemble('E',[a],output)
   self.assertFalse(output.exists() and any(output.rglob('kb_manifest.json')))
 def test_incompatible_schema_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);a=fixture(p/'a','doc-a',prefix='a');b=fixture(p/'b','doc-b',prefix='b');db=sqlite3.connect(b/'knowledge.sqlite');db.execute('create table unexpected_schema(extra text)');db.commit();db.close()
   with self.assertRaisesRegex(ValueError,'schemas'):assemble('E',[a,b],p/'out')

if __name__=='__main__':unittest.main()
