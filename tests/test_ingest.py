import copy
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from lxml import html
from app import Chunk, Citation, Query, Retriever, temporal, build_graph, run, DemoLLM
from ingest import load_bundle, ingest_root, expand_table

SAMPLE=Path(__file__).resolve().parents[1]/'sample_data'/'148_2026_QD_UBND'


class TestCrawlerData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records,cls.report=load_bundle(SAMPLE)
        cls.chunks=[Chunk.model_validate(x) for x in cls.records]
        cls.retriever=Retriever(cls.chunks)

    def row(self,criterion):
        return next(c for c in self.chunks if c.criterion==criterion)

    def test_all_criteria_and_provisions(self):
        self.assertEqual(len([c for c in self.chunks if c.chunk_type=='table_row']),36)
        self.assertEqual(len([c for c in self.chunks if c.chunk_type=='article']),3)
        self.assertTrue(any(c.article=='2' and c.clause=='2' and c.point=='a' for c in self.chunks))
        self.assertEqual(len({c.chunk_id for c in self.chunks}),len(self.chunks))

    def test_separate_group_thresholds(self):
        row=self.row('1.2')
        self.assertEqual(row.table_values,{'nhom_1':'≥99%','nhom_2':'≥99%','nhom_3':'≥98%'})
        self.assertIn('Thôn thuộc xã nhóm 3: ≥98%',row.content)

    def test_colspan_shared_value(self):
        row=self.row('1.4')
        self.assertEqual(row.table_values['nhom_1'],row.table_values['nhom_3'])
        self.assertIn('điện thoại',row.table_values['nhom_2'])

    def test_rowspan_category_preserved(self):
        self.assertIn('Hạ tầng kinh tế - xã hội',self.row('1.3').content)
        self.assertIn('Phát triển kinh tế nông thôn',self.row('2.3').content)

    def test_inline_footnotes_attached(self):
        self.assertIn('fn-2',self.row('1.1').footnote_ids)
        self.assertIn('không trồng',self.row('1.1').content)
        self.assertIn('fn-5',self.row('7.6').footnote_ids)
        self.assertIn('tối thiểu 2 km',self.row('7.6').content)

    def test_no_invented_url_and_unresolved_refs(self):
        self.assertIsNone(self.chunks[0].url)
        self.assertTrue(any(r['href'].startswith('/van-ban/') for r in self.chunks[0].references))
        self.assertTrue(all(not r['resolved'] for r in self.chunks[0].references))

    def test_hash_mismatch_reported_and_strict_rejected(self):
        self.assertEqual(self.report['hash_checks']['content.html'],'mismatch')
        self.assertEqual(self.report['hash_checks']['content.txt'],'mismatch')
        with self.assertRaises(ValueError):load_bundle(SAMPLE,strict_hash=True)

    def test_dates_inclusive_end_unknown_in_interval(self):
        c=self.row('1.2')
        self.assertEqual(c.effective_to,date(2031,1,1))
        self.assertEqual(temporal(c,date(2026,10,19)),'invalid')
        self.assertEqual(temporal(c,date(2030,12,31)),'unknown')
        self.assertEqual(temporal(c,date(2031,1,1)),'invalid')

    def test_default_retrieval_excludes_future_doc(self):
        ev=self.retriever.search(Query(text='điện sinh hoạt'),[],date(2026,10,19))
        self.assertEqual(ev,[])

    def test_document_scope_can_read_future_doc(self):
        ev=self.retriever.search(Query(text='điện sinh hoạt sản xuất xã nhóm 3',temporal_scope='document'),[],date(2026,10,19))
        self.assertTrue(any(e.criterion=='1.2' for e in ev))
        self.assertTrue(all(e.temporal_status=='invalid' for e in ev))

    def test_exact_annex_document_number(self):
        ref=Citation(citation_id='R1',document_id='148/2026/QĐ-UBND',annex='1',criterion='1.2',locality='Đà Nẵng')
        ev=self.retriever.search(Query(text='',purpose='resolve_citation',citation_id='R1'),[ref],date(2026,10,19))
        self.assertEqual(len(ev),1);self.assertEqual(ev[0].criterion,'1.2')
        self.assertIsNone(ev[0].article)

    def test_exact_article_parent_not_heading(self):
        ref=Citation(citation_id='R1',document_id='148/2026/QĐ-UBND',article='3')
        ev=self.retriever.search(Query(text='',purpose='resolve_citation',citation_id='R1'),[ref],date(2026,10,19))
        self.assertEqual(len(ev),1);self.assertEqual(ev[0].chunk_type,'article')
        self.assertIn('đến hết ngày 31 tháng 12 năm 2030',ev[0].content)

    def test_exact_clause_includes_points(self):
        ref=Citation(citation_id='R1',document_id='148/2026/QĐ-UBND',article='2',clause='2')
        ev=self.retriever.search(Query(text='',purpose='resolve_citation',citation_id='R1'),[ref],date(2026,10,19))
        self.assertEqual(len(ev),1)
        self.assertIn('a) Chủ trì',ev[0].content)
        self.assertIn('c) Tham mưu',ev[0].content)

    def test_locality_filter(self):
        ev=self.retriever.search(Query(text='điện',temporal_scope='document',locality='Hà Nội'),[],date(2026,12,1))
        self.assertEqual(ev,[])

    def test_document_scope_graph_is_explicit(self):
        graph=build_graph(self.retriever,DemoLLM())
        result=run(graph,'điện sinh hoạt xã nhóm 3',as_of='2026-10-19',temporal_scope='document',locality='Đà Nẵng')
        self.assertTrue(result['answer']);self.assertEqual(result['temporal_scope'],'document')
        self.assertTrue(all(r['verification_scope']=='document' for r in result['claim_reports']))
        self.assertTrue(any('không xác nhận' in x for x in result['limitations']))

    def test_snapshot_changes_and_stable_reimport(self):
        c2,r2=load_bundle(SAMPLE)
        self.assertEqual([c['chunk_id'] for c in c2],[c['chunk_id'] for c in self.records])
        with tempfile.TemporaryDirectory() as td:
            target=Path(td)
            for p in SAMPLE.iterdir(): target.joinpath(p.name).write_bytes(p.read_bytes())
            meta=json.loads((target/'metadata.json').read_text(encoding='utf-8'))
            meta['updatedDate']='2026-10-20T00:00:00'
            (target/'metadata.json').write_text(json.dumps(meta,ensure_ascii=False),encoding='utf-8')
            changed,_=load_bundle(target)
            self.assertNotEqual(changed[0]['version_id'],self.records[0]['version_id'])

    def test_txt_fallback_does_not_guess_table_columns(self):
        with tempfile.TemporaryDirectory() as td:
            target=Path(td)
            for name in ['metadata.json','content.txt','content.extraction.json']:
                target.joinpath(name).write_bytes((SAMPLE/name).read_bytes())
            records,report=load_bundle(target)
            self.assertEqual(len(records),3)
            self.assertFalse(any(c['chunk_type']=='table_row' for c in records))
            self.assertIn('TXT_FALLBACK_NO_TABLE_GEOMETRY',report['warnings'])

    def test_table_grid_simple(self):
        t=html.fromstring('<table><tr><td rowspan="2">A</td><td colspan="2">B</td></tr><tr><td>C</td><td>D</td></tr></table>')
        grid=expand_table(t)
        self.assertEqual([[x.text_content() for x in row] for row in grid],[['A','B','B'],['A','C','D']])

if __name__=='__main__':unittest.main()
