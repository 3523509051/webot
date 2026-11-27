"""Source-checkout integration setup. Scanning belongs to the bridge process."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import zlib


def build_configuration(core,bridge,snapshot,previous,agent_id):
    core,bridge=copy.deepcopy(core),copy.deepcopy(bridge)
    account_wxid=snapshot.get('account_wxid')
    if not isinstance(account_wxid,str) or not account_wxid or account_wxid.endswith('@chatroom'):raise ValueError('scan_account_missing')
    if previous.get('account_wxid') and previous['account_wxid']!=account_wxid:raise ValueError('wechat_account_changed')
    if agent_id not in {a['agent_id'] for a in core['agents']}:raise ValueError('agent_missing')
    raw=snapshot.get('groups')
    if not isinstance(raw,list) or not raw:raise ValueError('empty_group_scan')
    group_map={};targets={};labels={}
    account=str(bridge.get('astrbot',{}).get('self_id',10001))
    if not account.isdigit():raise ValueError('bridge_account_invalid')
    if previous.get('account_id') and previous['account_id']!=account:raise ValueError('bridge_account_changed')
    for row in raw:
        gid=row.get('id');name=row.get('name')
        if not isinstance(gid,str) or not gid.endswith('@chatroom') or not isinstance(name,str) or not name.strip():raise ValueError('invalid_group_scan')
        target=str(zlib.crc32(gid.encode('utf-8'))%10**9)
        if target in targets and targets[target]!=gid:raise ValueError('group_id_collision')
        historical=(previous.get('group_ids') or {}).get(target)
        if historical and historical!=gid:raise ValueError('historical_group_id_collision')
        targets[target]=gid;group_map[gid]=name.strip();labels[f'onebot11:{account}:group:{target}']=name.strip()[:120]
    channel=core.setdefault('channels',{}).setdefault('onebot11',{})
    channel.update(enabled=True,host='127.0.0.1',path=channel.get('path') or '/ws',token_env='PERO_ONEBOT_TOKEN')
    channel.setdefault('port',6200)
    control=core.setdefault('orchestration',{})
    old_routes=control.get('routes',[])
    bindings={str(row['target']):row['agent_id'] for row in old_routes if str(row['account'])==account and row['conversation_type']=='group'}
    # Refresh this account's group routes from this startup's snapshot.
    # Private routes and other accounts are not affected.
    routes=[row for row in old_routes if not (str(row['account'])==account
            and row['conversation_type']=='group' and str(row['target']) not in targets)]
    for target in sorted(targets):
        if target not in bindings:routes.append({'account':account,'conversation_type':'group','target':target,'agent_id':agent_id})
    control['routes']=routes
    prefix=f'onebot11:{account}:group:'
    allowed={cid for cid in control.get('allowed_conversations',[]) if not cid.startswith(prefix)}
    allowed.update(labels)
    old_known=set(previous.get('conversation_ids',[]))
    control.update(auto_process_enabled=True,auto_send_enabled=True,allowed_conversations=sorted(allowed))
    control['send_accounts']=sorted(set(control.get('send_accounts',[]))|{account})
    removed=set(bindings)-set(targets)
    other_targets={str(row['target']) for row in routes if str(row['account'])!=account}
    sends=set(control.get('send_targets',[]))-(removed-other_targets)
    sends.update(targets)
    control['send_targets']=sorted(sends)
    bridge.setdefault('astrbot',{}).update(self_id=int(account),ws_url=f'ws://127.0.0.1:{channel["port"]}{channel["path"]}',token='',token_env=channel['token_env'])
    bridge.setdefault('wechat',{}).update(groups=group_map,group_whitelist=[],self_wxid=account_wxid)
    bridge.setdefault('private_chat',{}).update(pero_url=f'http://127.0.0.1:{core.get("service",{}).get("port",8765)}',token_env='PERO_MANAGEMENT_TOKEN')
    state={'account_wxid':account_wxid,'account_id':account,'conversation_ids':sorted(old_known|set(labels)),
           'group_ids':{**previous.get('group_ids',{}),**targets}}
    return core,bridge,labels,state


def prepare(root,webot_root,config_path,state_dir,agent_id):
    from combo_launcher import atomic_write,LauncherError
    from pero.core.settings import Settings
    root=Path(root);webot=Path(webot_root).expanduser().resolve()
    bridge_dir=webot/'bridge';python=bridge_dir/'.venv/Scripts/python.exe'
    if not all(path.is_file() for path in [python,bridge_dir/'scan_groups.py',bridge_dir/'run_managed.py']):
        raise LauncherError('webot 路径或桥依赖不完整，请更新仓库并按 webot README 安装 bridge/.venv。')
    bridge_path=bridge_dir/'config.json'
    core=json.loads(config_path.read_text(encoding='utf-8-sig'))
    bridge=json.loads((bridge_path if bridge_path.exists() else bridge_dir/'config.example.json').read_text(encoding='utf-8-sig'))
    record=state_dir/'wechat-groups.json'
    previous=json.loads(record.read_text(encoding='utf-8')) if record.exists() else {}
    if not agent_id:agent_id=core['agents'][0]['agent_id']
    with tempfile.TemporaryDirectory(prefix='pero-group-scan-') as directory:
        output=Path(directory)/'groups.json'
        result=subprocess.run([str(python),str(bridge_dir/'scan_groups.py'),'--output',str(output)],cwd=bridge_dir,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90,
                              env={**os.environ,'PYTHONIOENCODING':'utf-8'},creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode or not output.exists():raise LauncherError('无法扫描已登录微信：请手动启动微信并登录，检查桥数据库目录。未覆盖现有配置。')
        snapshot=json.loads(output.read_text(encoding='utf-8'))
    core,bridge,labels,new_state=build_configuration(core,bridge,snapshot,previous,agent_id)
    settings=Settings.model_validate(core)
    if settings.service.host not in ('127.0.0.1','localhost'):raise LauncherError('微信组合启动当前要求核心监听 127.0.0.1 或 localhost。')
    labels_path=state_dir/'scope-labels.json'
    existing=json.loads(labels_path.read_text(encoding='utf-8')) if labels_path.exists() else {}
    existing.update(labels)
    # Back up both configurations before the first mutation; a failed write is rolled back.
    files={config_path:core,bridge_path:bridge,labels_path:existing,record:new_state}
    before={path:path.read_bytes() if path.exists() else None for path in files}
    backup=state_dir/'backups'/('pairing-'+str(__import__('time').time_ns()))
    backup.mkdir(parents=True,exist_ok=False)
    for index,(path,raw) in enumerate(before.items()):
        if raw is not None:atomic_write(backup/(str(index)+'.json'),raw)
    try:
        for path,value in files.items():atomic_write(path,json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8'))
    except BaseException:
        for path,raw in before.items():
            if raw is None:path.unlink(missing_ok=True)
            else:atomic_write(path,raw)
        raise
    print('全部扫描群接入已同步：'+str(len(labels))+' 个群。保留原有人格、群关闭设置和主人访问范围。',flush=True)
    return {'webot_root':webot,'python':python,'labels':labels_path,'token_env':core['channels']['onebot11']['token_env']}
