import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from document_bundle_builder.builder import build, inspect, logical_digest, sha, LAYERS

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / 'kb_sources/DOC-YAMZ-236M2-01'
DOC = 'DOC-YAMZ-236M2-01'


class BuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = inspect(INPUT, DOC)
        cls.temp = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temp.name) / 'output'
        cls.bundle = build(INPUT, cls.output, DOC)
        cls.manifest = json.loads((cls.bundle / 'manifest.json').read_text(encoding='utf8'))
        cls.validation = json.loads((cls.bundle / 'validation_report.json').read_text(encoding='utf8'))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_counts_and_readiness(self):
        self.assertEqual(self.validation['counts'], {'blocks':138,'chunks':148,'table_chunks':406,
            'passports':154,'figures':65,'regions':69,'text_table_links':236,'text_figure_links':84})
        self.assertFalse(self.manifest['runtime_enabled'])
        self.assertEqual(self.validation['unlinked_table_records'], 189)
        with sqlite3.connect(self.bundle / 'knowledge.sqlite') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM bundle_search_records').fetchone()[0],708)
            self.assertEqual(db.execute('SELECT count(*) FROM bundle_search_records WHERE runtime_enabled != 0').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM bundle_search_records WHERE kind='table' AND future_candidate=1 AND source_id NOT IN (SELECT table_chunk_id FROM text_table_links__semantic_links)").fetchone()[0],185)
            self.assertEqual(dict(db.execute('SELECT relation_type,count(*) FROM text_table_links__semantic_links GROUP BY relation_type')),{'complement':220,'same_content':16})

    def test_full_table_contents_and_source_ddl_preserved(self):
        with sqlite3.connect(self.bundle / 'knowledge.sqlite') as db:
            records = db.execute('SELECT layer,source_name,output_name,source_ddl,row_count,logical_sha256 FROM bundle_source_schema').fetchall()
            self.assertTrue(records)
            for layer,source,name,ddl,count,digest in records:
                self.assertTrue(ddl)
                self.assertEqual(db.execute('SELECT count(*) FROM "'+name+'"').fetchone()[0],count)
                self.assertEqual(logical_digest(db,name),digest)
            self.assertEqual({r[0] for r in records}, set(LAYERS))

    def test_original_files_and_pngs(self):
        self.assertEqual(sha(self.bundle/'source.pdf'),sha(INPUT/'source.pdf'))
        with sqlite3.connect(self.bundle/'knowledge.sqlite') as db:
            for path,digest in db.execute('SELECT asset_path,asset_sha256 FROM figure_layer__figure_regions'):
                self.assertEqual(sha(self.bundle/path),digest)
        self.assertEqual(inspect(INPUT,DOC)['hashes'],self.before['hashes'])

    def test_warnings_and_scope_are_preserved(self):
        w={x['type']:x for x in self.validation['warnings']}
        self.assertEqual(len(w['region_source_object_missing']['ids']),19)
        self.assertEqual(len(w['figure_source_fingerprint_mismatch']['ids']),1)
        self.assertGreater(len(w['historical_paths_unresolved']['references']),0)
        with sqlite3.connect(self.bundle/'knowledge.sqlite') as db:
            self.assertEqual(db.execute("SELECT count(*) FROM passport_layer__passports WHERE scope_status='unresolved'").fetchone()[0],5)
            self.assertEqual(db.execute("SELECT count(*) FROM table_layer__final_readiness_03_4 WHERE future_candidate=0").fetchone()[0],4)

    def test_deterministic_rebuild(self):
        again = build(INPUT,self.output,DOC)
        self.assertEqual(again,self.bundle)
        self.assertEqual(json.loads((again/'manifest.json').read_text(encoding='utf8')),self.manifest)

    def test_corrupt_input_no_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)/'input'
            source.mkdir()
            shutil.copyfile(INPUT/'source.pdf',source/'source.pdf')
            shutil.copytree(INPUT/'prepared',source/'prepared')
            with (source/'prepared/table_layer.sqlite').open('r+b') as f:
                f.seek(0); f.write(b'BROKEN')
            output = Path(temp)/'output'
            with self.assertRaises(ValueError):
                build(source,output,DOC)
            self.assertFalse(output.exists() and any(output.rglob('build_*')))

    def test_wrong_document_id_and_invalid_asset(self):
        with self.assertRaises(ValueError):
            inspect(INPUT,'wrong-id')
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)/'input'
            source.mkdir()
            shutil.copyfile(INPUT/'source.pdf',source/'source.pdf')
            shutil.copytree(INPUT/'prepared',source/'prepared')
            png = next((source/'prepared/assets').glob('*.png'))
            png.write_bytes(b'corrupt')
            with self.assertRaises(ValueError):
                inspect(source,DOC)


if __name__ == '__main__':
    unittest.main()
