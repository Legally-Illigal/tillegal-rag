"""Import crawler bundles. No network calls; metadata is a source claim, not legal verification."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from lxml import html


def norm(s):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', s)).strip()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_date(value):
    return date.fromisoformat(value[:10]) if value else None


def expand_table(table):
    """Expand rowspan/colspan to a logical grid, preserving source cells."""
    occupied = {}; rows = []
    for ri,tr in enumerate(table.xpath('./tr|./thead/tr|./tbody/tr|./tfoot/tr')):
        col=0
        for cell in tr.xpath('./td|./th'):
            while (ri,col) in occupied:
                col+=1
            rs,cs=int(cell.get('rowspan','1')),int(cell.get('colspan','1'))
            if not (1<=rs<=1000 and 1<=cs<=100):
                raise ValueError('Unsupported table span')
            for dr in range(rs):
                for dc in range(cs):
                    if (ri+dr,col+dc) in occupied:
                        raise ValueError('Overlapping table cells')
                    occupied[ri+dr,col+dc]=cell
            col+=cs
        width=max((c+1 for r,c in occupied if r==ri),default=0)
        rows.append([occupied.get((ri,c)) for c in range(width)])
    return rows


def text_of(node):
    return norm(node.text_content()) if node is not None else ''


def load_bundle(folder: str | Path, strict_hash=False):
    folder=Path(folder); meta=json.loads((folder/'metadata.json').read_text(encoding='utf-8'))
    if not meta.get('id') or not meta.get('docNum') or not meta.get('title'):
        raise ValueError('metadata requires id, docNum, title')
    hp,tp,ep=(folder/x for x in ('content.html','content.txt','content.extraction.json'))
    raw_html=hp.read_bytes() if hp.exists() else b''
    raw_text=tp.read_bytes() if tp.exists() else b''
    if not raw_html and not raw_text:
        raise ValueError('Missing nonempty content.html/content.txt')
    manifest=json.loads(ep.read_text(encoding='utf-8')) if ep.exists() else {}
    hashes={name:digest(raw) for name,raw in [('content.html',raw_html),('content.txt',raw_text)] if raw}
    warnings=[]; checks={}
    for field,name in [('source_sha256','content.html'),('text_sha256','content.txt')]:
        expected=manifest.get(field); actual=hashes.get(name)
        checks[name]='unknown' if not expected or not actual else 'matched' if expected==actual else 'mismatch'
        if checks[name]=='mismatch':
            warnings.append(f'HASH_MISMATCH:{name}')
    if strict_hash and any(v=='mismatch' for v in checks.values()):
        raise ValueError('Extraction hash mismatch')
    from_date,to_date=read_date(meta.get('effFrom')),read_date(meta.get('effTo'))
    if from_date and to_date and to_date<from_date:
        raise ValueError('effTo before effFrom')
    # Date-only metadata effTo is inclusive; internal model uses exclusive end.
    to_exclusive=to_date+timedelta(days=1) if to_date else None
    snapshot=digest(json.dumps({'metadata':meta,'files':hashes},ensure_ascii=False,sort_keys=True).encode())
    refs=[]; chunks=[]
    base=dict(document_id=meta['id'],document_number=meta['docNum'],version_id='snapshot-'+snapshot[:16],
        title=meta['title'],jurisdiction='VN',locality=(meta.get('organization') or {}).get('name'),
        locality_code=(meta.get('organization') or {}).get('code'),agency=meta.get('agencyName'),
        issue_date=read_date(meta.get('issueDate')),effective_from=from_date,effective_to=to_exclusive,
        source_effective_to=to_date,validity_known=False,source_status=(meta.get('effStatus') or {}).get('name'),
        source_type='crawler_snapshot',url=None,source_files=[p.name for p in (hp,tp,ep,folder/'metadata.json') if p.exists()],
        source_hashes=hashes,extraction_checks=checks,ingest_warnings=warnings,
        updated_at=meta.get('updatedDate'),version_kind='content_snapshot_not_legal_version')
    for key in ('url','source_url','sourceUrl'):
        if isinstance(meta.get(key),str) and meta[key].startswith(('https://','http://')):
            base['url']=meta[key];break
    warnings.append('TEMPORAL_METADATA_NOT_INDEPENDENTLY_VERIFIED')
    if not base['url']:
        warnings.append('SOURCE_URL_MISSING')

    def add(location, content, kind, article=None,clause=None,point=None,**extra):
        content=content.strip()
        if not content:return
        cid=meta['id']+':'+base['version_id']+':'+location+':'+digest(content.encode())[:12]
        chunks.append({**base,'chunk_id':cid,'article':article,'clause':clause,'point':point,
            'location_id':location,'chunk_type':kind,'content':content,**extra})

    if raw_html:
        root=html.fromstring(raw_html)
        for bad in root.xpath('//script|//style|//noscript'):
            bad.drop_tree()
        for a in root.xpath('//a[@href]'):
            if a.get('href').startswith('#'):continue
            ref={'label':text_of(a),'href':a.get('href'),'resolved':False}
            if ref not in refs:refs.append(ref)
        footnotes={n.get('id'):text_of(n).rstrip(' ↩') for n in root.xpath('//*[@id and contains(concat(" ",normalize-space(@class)," ")," footnote-item ")]')}
        notes=[text_of(p) for p in root.xpath('//p') if text_of(p).startswith('Ghi chú:')]
        current_article=current_clause=None; article_title='';clause_text=''
        parts=[];head='';loc='';kind='';point=None;anchors=[]
        def flush():
            if parts:
                add(loc,'\n'.join(parts),kind,current_article,current_clause,point,
                    parent_location_id=f'article:{current_article}' if current_clause else None,source_anchors=list(anchors))
        for p in root.xpath('//p'):
            # Table and footnote paragraphs have their own structured parsers.
            if p.xpath('ancestor::table|ancestor::*[contains(concat(" ",normalize-space(@class)," ")," footnotes ")]'):
                continue
            txt=text_of(p)
            if not txt:continue
            cls=p.get('class','').split()
            am=re.match(r'^Điều\s+(\d+[a-zđ]?)\.',txt,re.I)
            cm=re.match(r'^(\d+)\.\s',txt)
            pm=re.match(r'^([a-zđ])\)\s',txt,re.I)
            if 'prov-article' in cls or am:
                flush();parts=[]
                if not am:raise ValueError('Unrecognized article heading')
                current_article,current_clause,point=am[1],None,None
                article_title=txt;loc=f'article:{current_article}';kind='article_heading';parts=[txt];anchors=[p.get('id')] if p.get('id') else []
            elif current_article and ('prov-clause' in cls or cm):
                flush();current_clause=cm[1] if cm else None;point=None;clause_text=txt
                loc=f'article:{current_article}/clause:{current_clause}';kind='clause';parts=[article_title,txt];anchors=[p.get('id')] if p.get('id') else []
            elif current_article and ('prov-item' in cls or pm):
                flush();point=pm[1] if pm else None
                loc=f'article:{current_article}/clause:{current_clause}/point:{point}';kind='point'
                parts=[article_title,clause_text,txt];anchors=[p.get('id')] if p.get('id') else []
            elif txt=='Phụ lục':
                flush();parts=[];current_article=current_clause=point=None
            elif current_article:
                parts.append(txt)
        flush()
        # A clause citation must include its child points, not just its title.
        for clause_chunk in [c for c in chunks if c['chunk_type']=='clause']:
            children=[c for c in chunks if c['chunk_type']=='point' and c['article']==clause_chunk['article'] and c['clause']==clause_chunk['clause']]
            if children:
                lines=clause_chunk['content'].splitlines()
                for child in children:
                    for line in child['content'].splitlines():
                        if line not in lines:lines.append(line)
                clause_chunk['content']='\n'.join(lines)
                clause_chunk['source_anchors']+= [a for c in children for a in c.get('source_anchors',[])]
                clause_chunk['chunk_id']=meta['id']+':'+base['version_id']+':'+clause_chunk['location_id']+':'+digest(clause_chunk['content'].encode())[:12]
        # Full article parents preserve all clauses when an article is cited.
        for article_id in sorted({c['article'] for c in chunks if c['article']}):
            children=[c for c in chunks if c['article']==article_id]
            lines=[]
            for child in children:
                for line in child['content'].splitlines():
                    if line not in lines:lines.append(line)
            add('article:'+article_id,'\n'.join(lines),'article',article=article_id,source_anchors=[a for c in children for a in c.get('source_anchors',[])])
        table_count=0
        for ti,table in enumerate(root.xpath('//table'),1):
            grid=expand_table(table)
            # Recognize this source's 7-column criteria table by labels, not table index.
            if not grid or 'Nội dung tiêu chí' not in text_of(table):continue
            if not all(len(row)==7 for row in grid):
                warnings.append(f'UNSUPPORTED_TABLE_LAYOUT:{ti}');continue
            table_count+=1
            for ri,row in enumerate(grid,1):
                cells=[text_of(c) for c in row]
                match=re.match(r'^(\d+\.\d+)\.',cells[2])
                if not match:continue
                criterion=match[1];location=f'annex:1/table:{ti}/criterion:{criterion}'
                lines=['Phụ lục — '+meta['docNum'],f'Tên tiêu chí: {cells[1]}',f'Nội dung tiêu chí: {cells[2]}']
                values={f'nhom_{g}':cells[2+g] for g in (1,2,3)}
                for g in (1,2,3):lines.append(f'Thôn thuộc xã nhóm {g}: {values[f"nhom_{g}"]}')
                lines.append('Nội dung, văn bản áp dụng, đánh giá: '+cells[6])
                foot_ids=sorted({a.get('href')[1:] for c in row if c is not None for a in c.xpath('.//a[starts-with(@href,"#fn-")]')} | {'fn-'+n for value in cells for n in re.findall(r'\[(\d+)\]',value)})
                for fid in foot_ids:
                    if fid in footnotes:lines.append(f'Chú thích {fid}: {footnotes[fid]}')
                    else:warnings.append('MISSING_FOOTNOTE:'+fid)
                lines.extend(notes)
                add(location,'\n'.join(lines),'table_row',annex='1',criterion=criterion,table_values=values,
                    source_anchors=[f'table:{ti}/row:{ri}'],footnote_ids=foot_ids,
                    # These named fields are extraction evidence, not quotes from a flattened TXT.
                    original_cells=cells,content_representation='labeled_html_table_row')
        for fid,txt in footnotes.items():add('footnote:'+fid,txt,'footnote',source_anchors=[fid])
        for i,txt in enumerate(notes,1):add(f'annex_note:{i}',txt,'annex_note')
        if not chunks:raise ValueError('HTML has no recognized provisions or criteria; manual parser review required')
        if 'Phụ lục' in text_of(root) and not table_count:warnings.append('ANNEX_TABLE_NOT_PARSED')
    else:
        # Text fallback keeps articles intact; cannot reconstruct annex column associations.
        warnings.append('TXT_FALLBACK_NO_TABLE_GEOMETRY')
        txt=raw_text.decode('utf-8-sig')
        txt=txt.split('\nPhụ lục',1)[0]
        sections=re.split(r'(?m)(?=^Điều\s+\d+[a-zđ]?\.)',txt)
        for section in sections:
            m=re.match(r'^Điều\s+(\d+[a-zđ]?)\.',section)
            if m:add('article:'+m[1],section,'article',article=m[1])
        if not chunks:raise ValueError('TXT has no recognized article headings')
    for c in chunks:
        c['references']=refs;c['ingest_warnings']=list(dict.fromkeys(warnings))
    report={'document_id':meta['id'],'document_number':meta['docNum'],'snapshot_id':base['version_id'],
        'chunks':len(chunks),'counts':{k:sum(c['chunk_type']==k for c in chunks) for k in sorted({c['chunk_type'] for c in chunks})},
        'hash_checks':checks,'warnings':list(dict.fromkeys(warnings)),'references':refs,'input_folder':str(folder)}
    return chunks,report


def ingest_root(root,strict_hash=False):
    root=Path(root)
    folders=[root] if (root/'metadata.json').is_file() else sorted(p.parent for p in root.rglob('metadata.json'))
    if not folders:raise ValueError('No metadata.json bundles found')
    chunks=[];reports=[]
    for folder in folders:
        c,r=load_bundle(folder,strict_hash);chunks.extend(c);reports.append(r)
    ids=[c['chunk_id'] for c in chunks]
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate bundles/chunk IDs')
    return chunks,reports


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input',required=True);p.add_argument('--output',default='corpus.jsonl');p.add_argument('--strict-hash',action='store_true')
    a=p.parse_args();chunks,reports=ingest_root(a.input,a.strict_hash)
    Path(a.output).write_text(''.join(json.dumps(c,ensure_ascii=False,default=str)+'\n' for c in chunks),encoding='utf-8')
    Path(a.output+'.report.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({'chunks':len(chunks),'documents':len(reports),'report':a.output+'.report.json'},ensure_ascii=False))
