"""Cloud entrypoint. Generate fresh local keys only for a fresh preview database."""
import os
from pathlib import Path
import secrets


def prepare():
    os.umask(0o077)
    root=Path(__file__).resolve().parents[1]
    os.chdir(root)
    os.environ.setdefault('DEPLOYMENT_MODE','hosted')
    os.environ.setdefault('APP_ENV','production')
    os.environ.setdefault('SCHEDULER_ENABLED','false')
    os.environ.setdefault('TTS_ENABLED','false')
    host=os.environ.get('RENDER_EXTERNAL_HOSTNAME','').strip()
    hosts=[v.strip() for v in os.environ.get('ALLOWED_HOSTS','localhost,127.0.0.1').split(',') if v.strip()]
    if host and host not in hosts:hosts.append(host)
    os.environ['ALLOWED_HOSTS']=','.join(hosts)
    data=root/'data';data.mkdir(exist_ok=True,mode=0o700)
    key=root/'secrets/database.key'
    key.parent.mkdir(exist_ok=True,mode=0o700)
    if not key.exists():
        if (data/'morning_mail_pa.db').exists():raise RuntimeError('Database key missing; refusing existing database')
        with key.open('xb') as f:f.write(secrets.token_bytes(32))
    os.environ.setdefault('DATABASE_KEY_FILE',str(key))
    encoded=os.environ.get('PROVIDER_CREDENTIAL_KEY_BASE64','')
    if encoded:
        import base64
        import hmac
        material=base64.b64decode(encoded,validate=True)
        if len(material)!=32:raise RuntimeError('Provider key must decode to 32 bytes')
        directory=root/'secrets/provider-credential-keys'
        directory.mkdir(exist_ok=True,mode=0o700)
        target=directory/'1.key'
        if target.exists():
            if not hmac.compare_digest(target.read_bytes(),material):raise RuntimeError('Provider key changed; refusing unsafe replacement')
        else:
            with target.open('xb') as f:f.write(material)
        os.environ['PROVIDER_CREDENTIAL_KEY_DIRECTORY']=str(directory)
    return root


if __name__=='__main__':
    prepare()
    os.execvp('python',['python','-m','uvicorn','deploy.cloud_app:app','--host','0.0.0.0',
                       '--port',str(int(os.environ.get('PORT','10000'))),'--no-access-log'])
