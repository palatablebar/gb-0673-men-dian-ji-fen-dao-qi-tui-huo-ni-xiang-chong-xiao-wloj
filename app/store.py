import sqlite3, json
from pathlib import Path
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("CREATE TABLE IF NOT EXISTS compensations (run_id TEXT NOT NULL, comp_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id,comp_id))")
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    merge_publish(path,result)
    return result

def merge_publish(path,result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        published={r['comp_id']: json.loads(r['payload']) for r in con.execute('SELECT comp_id,payload FROM compensations WHERE run_id=?',(run_id,))}
    result['published_compensations']=published
    for row in result.get('compensation',{}).get('rows',[]):
        if row['comp_id'] in published:
            row['publish_status']='已发布'

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def publish_compensations(path, run_id, revision, comp_ids=None):
    # 单事务：核对失败或版本陈旧整体回滚，旧版余额原样保留，重复执行不新增。
    with connect(path) as con:
        row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
        if not isinstance(revision,int) or revision<1:
            raise BusinessError('BAD_REVISION','版本号须为正整数')
        if row['revision']!=revision:
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
        result=json.loads(row['result'])
        comp=result.get('compensation') or {}
        if not comp.get('check_passed'):
            raise BusinessError('COMPENSATION_CHECK_FAILED','补偿核对未通过，保留旧版余额',409)
        balance=result.get('balance') or {}
        if not (balance.get('row_balanced') and balance.get('points_balanced')):
            raise BusinessError('LEDGER_UNBALANCED','积分守恒核对失败，保留旧版余额',409)
        candidates={r['comp_id']: r for r in comp.get('rows',[]) if r['comp_points']>0}
        targets=list(candidates) if not comp_ids else [c for c in comp_ids if c in candidates]
        if comp_ids and any(c not in candidates for c in comp_ids):
            raise BusinessError('COMPENSATION_NOT_FOUND','补偿批次不存在或已关闭',404)
        existing={r['comp_id'] for r in con.execute('SELECT comp_id FROM compensations WHERE run_id=?',(run_id,))}
        fresh=[c for c in targets if c not in existing]
        for comp_id in fresh:
            con.execute('INSERT INTO compensations(run_id,comp_id,payload) VALUES(?,?,?)',
                        (run_id,comp_id,json.dumps(candidates[comp_id],ensure_ascii=False)))
        if fresh:
            con.execute('UPDATE runs SET revision=revision+1 WHERE id=?',(run_id,))
        con.commit()
    return get(path,run_id), [c for c in targets if c in existing], fresh
