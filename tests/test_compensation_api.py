from fastapi.testclient import TestClient
from app import main, store
from app.engine import evaluate
from copy import deepcopy
import json

def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DB_PATH',str(tmp_path/'test.sqlite'))
    return TestClient(main.app)

def _publish(c, rid, revision, comp_ids=None):
    payload={'revision':revision}
    if comp_ids is not None: payload['comp_ids']=comp_ids
    return c.post(f'/api/runs/{rid}/compensations/publish',json=payload)

def test_publish_flow_and_idempotent(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch); run=c.post('/api/runs',json={}).json(); rid=run['manifest']['run_id']
    pending=[r['comp_id'] for r in run['compensation']['rows'] if r['publish_status']=='待发布']
    assert len(pending)==15  # 16 批中 E1 到期关闭
    first=_publish(c,rid,1)
    assert first.status_code==200
    data=first.json()
    assert len(data['published'])==15 and data['duplicated']==[] and data['revision']==2
    # 全部回读为已发布。
    assert all(r['publish_status']=='已发布' for r in data['compensation_rows'] if r['comp_points']>0)
    # 重复执行不新增。
    second=_publish(c,rid,2)
    assert second.status_code==200
    assert second.json()['published']==[] and len(second.json()['duplicated'])==15
    assert second.json()['revision']==2
    # 持久层确实只有15条发布记录。
    with store.connect(main.DB_PATH) as con:
        assert con.execute('SELECT COUNT(*) FROM compensations WHERE run_id=?',(rid,)).fetchone()[0]==15

def test_stale_revision_rejected_and_old_balance_kept(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch); run=c.post('/api/runs',json={}).json(); rid=run['manifest']['run_id']
    # 先成功发布一次，revision 升到2。
    assert _publish(c,rid,1).status_code==200
    # 陈旧 revision 被拒，且不新增任何记录。
    stale=_publish(c,rid,1)
    assert stale.status_code==409 and stale.json()['code']=='STALE_VERSION'
    with store.connect(main.DB_PATH) as con:
        assert con.execute('SELECT COUNT(*) FROM compensations WHERE run_id=?',(rid,)).fetchone()[0]==15

def test_check_failure_keeps_legacy_balance(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch); run=c.post('/api/runs',json={}).json(); rid=run['manifest']['run_id']
    # 直接破坏持久化结果的守恒标记，模拟核对失败：发布必须整体回滚。
    with store.connect(main.DB_PATH) as con:
        row=con.execute('SELECT result FROM runs WHERE id=?',(rid,)).fetchone()
        result=json.loads(row['result']); result['balance']['points_balanced']=False
        con.execute('UPDATE runs SET result=? WHERE id=?',(json.dumps(result,ensure_ascii=False),rid))
    resp=_publish(c,rid,1)
    assert resp.status_code==409 and resp.json()['code']=='LEDGER_UNBALANCED'
    with store.connect(main.DB_PATH) as con:
        assert con.execute('SELECT COUNT(*) FROM compensations WHERE run_id=?',(rid,)).fetchone()[0]==0
        assert con.execute('SELECT revision FROM runs WHERE id=?',(rid,)).fetchone()[0]==1

def test_compensation_check_failure_blocks_publish(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch); run=c.post('/api/runs',json={}).json(); rid=run['manifest']['run_id']
    with store.connect(main.DB_PATH) as con:
        row=con.execute('SELECT result FROM runs WHERE id=?',(rid,)).fetchone()
        result=json.loads(row['result']); result['compensation']['check_passed']=False
        con.execute('UPDATE runs SET result=? WHERE id=?',(json.dumps(result,ensure_ascii=False),rid))
    resp=_publish(c,rid,1)
    assert resp.status_code==409 and resp.json()['code']=='COMPENSATION_CHECK_FAILED'

def test_publish_single_and_closed_lot_rejected(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch); run=c.post('/api/runs',json={}).json(); rid=run['manifest']['run_id']
    closed=next(r['comp_id'] for r in run['compensation']['rows'] if r['publish_status']=='已关闭')
    # 已关闭批次不可发布。
    resp=_publish(c,rid,1,[closed])
    assert resp.status_code==404 and resp.json()['code']=='COMPENSATION_NOT_FOUND'
    # 指定单个待发布批次。
    one=next(r['comp_id'] for r in run['compensation']['rows'] if r['publish_status']=='待发布')
    ok=_publish(c,rid,1,[one])
    assert ok.status_code==200 and ok.json()['published']==[one]
    # 再次发布同一批为重复，不新增、不改 revision。
    again=_publish(c,rid,2,[one])
    assert again.status_code==200 and again.json()['published']==[] and again.json()['duplicated']==[one]

def test_bad_revision_payload(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch); run=c.post('/api/runs',json={}).json(); rid=run['manifest']['run_id']
    assert c.post(f'/api/runs/{rid}/compensations/publish',json={'revision':0}).status_code==422
    assert c.post(f'/api/runs/{rid}/compensations/publish',json={'revision':1,'comp_ids':'X'}).status_code==422

def test_refund_page_renders_compensation_panel(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch); run=c.post('/api/runs',json={}).json(); rid=run['manifest']['run_id']
    html=c.get(f'/refund?run_id={rid}').text
    assert '退货补偿批次' in html and '原批次到期损失' in html and '补偿可用额' in html
    assert '发布状态' in html and '沿用原到期' in html
    # 重算双闸门通过 overrides 打开后，页面展示重算裁决与新增负债。
    rec=c.post('/api/runs',json={'overrides':{'compensation_policy':{'same_day_consumption':True,'finance_liability_ack':True,'window_days':30}}}).json()
    html2=c.get(f'/refund?run_id={rec["manifest"]["run_id"]}').text
    assert '重算可用期' in html2 and '财务确认新增负债：是' in html2
