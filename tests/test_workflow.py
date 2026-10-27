import unittest
from datetime import date
from app import *


def corpus():
    return [Chunk.model_validate_json(x) for x in Path('demo_corpus.jsonl').read_text(encoding='utf-8').splitlines()]


class TestWorkflow(unittest.TestCase):
    def test_demo_passes(self):
        r=run(build_graph(Retriever(corpus()),DemoLLM()),'quy tắc DEMO')
        self.assertTrue(r['answer']); self.assertTrue(all(x['verification_status']=='VERIFIED' for x in r['claim_reports']))

    def test_empty_not_success(self):
        r=run(build_graph(Retriever([]),DemoLLM()),'không có')
        self.assertEqual(r['answer'],''); self.assertEqual(r['judge_status'],'INSUFFICIENT')

    def test_exact_preserves_old_citation(self):
        r=Retriever(corpus()); ref=Citation(citation_id='R1',document_id='DEMO',version_id='old',article='1')
        ev=r.search(Query(text='DEMO',purpose='resolve_citation',citation_id='R1'),[ref],date(2026,1,1))
        self.assertEqual(ev[0].version_id,'old'); self.assertEqual(ev[0].temporal_status,'invalid')
        self.assertTrue(all(e.version_id!='old' for e in r.search(Query(text='quy tắc'),[],date(2026,1,1))))

    def test_verify_keeps_wrong_original(self):
        class V(DemoLLM):
            def plan(self,s):
                return Plan(issues=['test'],queries=[Query(text='DEMO',purpose='resolve_citation',citation_id='R1')],
                    claims=[Claim(claim_id='C1',text='Nội dung sai',citation_ids=['R1'])],
                    citations=[Citation(citation_id='R1',document_id='DEMO',version_id='v1',article='1')])
        r=run(build_graph(Retriever(corpus()),V()),'nguyên văn','verify')
        self.assertIsNone(r['answer']); self.assertEqual(r['original_input'],'nguyên văn')
        self.assertEqual(r['claim_reports'][0]['verification_status'],'INVALID')

    def test_judge_error_is_error(self):
        class Broken(DemoLLM):
            def judge(self,s): raise TimeoutError()
        r=run(build_graph(Retriever(corpus()),Broken()),'DEMO')
        self.assertEqual(r['judge_status'],'ERROR')

    def test_bounded_loop(self):
        class Loop(DemoLLM):
            def judge(self,s):
                return Decision(status='INSUFFICIENT',missing_items=['gap'],next_queries=[Query(text='mới '+str(s['rounds']))])
        r=run(build_graph(Retriever(corpus()),Loop()),'DEMO',max_rounds=1)
        self.assertEqual(r['rounds'],1); self.assertEqual(r['stop_reason'],'search_budget_exhausted')
        self.assertEqual(r['judge_status'],'INSUFFICIENT')

    def test_unknown_time_not_verified(self):
        chunks=corpus()
        for c in chunks: c.validity_known=False
        class Unknown(DemoLLM):
            def judge(self,s): return Decision(status='SUFFICIENT',usable_evidence_ids=list(s['evidence']))
        r=run(build_graph(Retriever(chunks),Unknown()),'DEMO')
        self.assertEqual(r['answer'],''); self.assertTrue(all(x['verification_status']=='UNVERIFIED' for x in r['claim_reports']))

    def test_verifier_error_removes_sentence(self):
        class Broken(DemoLLM):
            def entail(self,*a): raise TimeoutError()
        r=run(build_graph(Retriever(corpus()),Broken()),'DEMO')
        self.assertEqual(r['answer'],''); self.assertTrue(all(x['support_label']=='verifier_error' for x in r['claim_reports']))

    def test_dense_path_rrf(self):
        import numpy as np
        class Encoder:
            def encode(self,texts,**kw): return np.array([[1.,0.] for _ in texts])
        result=Retriever(corpus(),Encoder()).search(Query(text='khác từ vựng'),[],date(2026,1,1))
        self.assertTrue(result); self.assertIn('dense',result[0].retrieval_methods)

if __name__=='__main__': unittest.main()
