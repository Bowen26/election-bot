"""Public feed staging. Unreviewed third-party rows never become model inputs."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import urllib.request

from .clients import NoRedirect
from .polling import MAX_BYTES, digest

PUBLIC_URL='https://decisionlabs.ai/api/export/polls?format=json'


def stage_public(runtime, now=None):
    now=time.time() if now is None else now
    request=urllib.request.Request(PUBLIC_URL,headers={'Accept':'application/json','User-Agent':'ElectionBot-Research/1.0'})
    with urllib.request.build_opener(NoRedirect()).open(request,timeout=10) as response:
        body=response.read(MAX_BYTES+1)
    if len(body)>MAX_BYTES:raise ValueError('Public poll feed exceeds 4 MB')
    data=json.loads(body)
    polls=data['polls']
    if not isinstance(polls,list) or len(polls)>5000:raise ValueError('Unsupported public poll feed')
    valid=[]
    for p in polls:
        try:valid.append(datetime.strptime(p['date'],'%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())
        except (ValueError,KeyError,TypeError):pass
    newest=max(valid) if valid else None
    summary={'source':PUBLIC_URL,'attribution':'Decision Labs (decisionlabs.ai), CC BY 4.0; underlying source licenses apply',
        'received_at':now,'rows':len(polls),'newest_poll_at':newest,
        'newest_age_days':(now-newest)/86400 if newest is not None else None,
        'model_inputs_imported':0,'status':'staged_only',
        'reason':'Requires verified general-election stage, candidate identity, fieldwork and publication timestamps; provider date alone is insufficient.'}
    directory=Path(runtime)/'poll-staging';directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=directory/(digest(data)+'.json')
    if not path.exists():
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as stream:
            json.dump({'summary':summary,'raw':data},stream,allow_nan=False);stream.flush();os.fsync(stream.fileno())
    summary['file']=str(path)
    return summary
