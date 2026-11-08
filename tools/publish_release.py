"""Publish the already-built and tested package, using local GitHub credentials."""
import argparse,json,os,subprocess,urllib.request,urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
REPO='trackk81/rea-pencil-for-blender'

def token():
    value=os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if value:return value
    result=subprocess.run(['git','credential','fill'],input='protocol=https\nhost=github.com\n\n',text=True,capture_output=True,timeout=30,check=True)
    credentials=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    if not credentials.get('password'):raise RuntimeError('GitHub authentication unavailable')
    return credentials['password']

def request(url,method='GET',data=None,content_type='application/json'):
    headers={'Authorization':'Bearer '+token(),'User-Agent':'rea-pencil-release','Accept':'application/vnd.github+json'}
    if data is not None:headers['Content-Type']=content_type
    body=json.dumps(data).encode() if isinstance(data,dict) else data
    with urllib.request.urlopen(urllib.request.Request(url,data=body,headers=headers,method=method),timeout=60) as response:return json.load(response)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('create-repo','publish'));args=parser.parse_args()
    if args.action=='create-repo':
        try:repo=request('https://api.github.com/repos/'+REPO)
        except urllib.error.HTTPError as exc:
            if exc.code!=404:raise
            repo=request('https://api.github.com/user/repos','POST',{'name':REPO.split('/')[1],'description':'Independent node-driven line rendering for Blender 4.4, with restart-safe GitHub updates.','private':False,'auto_init':False})
        print(json.dumps({'repository':repo['html_url'],'private':repo['private']}));return
    metadata=json.loads((ROOT/'lea_line/release.json').read_text());version='.'.join(map(str,metadata['version']));tag='v'+version
    archive=ROOT/'dist'/('rea_pencil_for_blender-'+version+'.zip');checksum=archive.with_suffix('.sha256')
    if not archive.exists() or not checksum.exists():raise RuntimeError('Build and test the release ZIP first')
    # Draft first; publish only after every asset has been uploaded successfully.
    release=request('https://api.github.com/repos/'+REPO+'/releases','POST',{'tag_name':tag,'target_commitish':'main','name':'rea pencil for blender '+version,'draft':True,'prerelease':False,'body':(ROOT/'RELEASE_NOTES.md').read_text(encoding='utf-8')})
    for path,mime in ((archive,'application/zip'),(checksum,'text/plain')):
        request('https://uploads.github.com/repos/'+REPO+'/releases/'+str(release['id'])+'/assets?name='+path.name,'POST',path.read_bytes(),mime)
    release=request(release['url'],'PATCH',{'draft':False,'make_latest':'true'})
    print(json.dumps({'release':release['html_url'],'assets':[{'name':a['name'],'digest':a.get('digest'),'url':a['browser_download_url']} for a in release['assets']]},indent=2))

if __name__=='__main__':main()
