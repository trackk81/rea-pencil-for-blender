"""Build an installable release ZIP from the selected repository source."""
from pathlib import Path
import ast,hashlib,json,zipfile
ROOT=Path(__file__).resolve().parents[1]
metadata=json.loads((ROOT/'lea_line/release.json').read_text())
version='.'.join(map(str,metadata['version']))
for p in (ROOT/'lea_line').glob('*.py'):ast.parse(p.read_text(encoding='utf-8'),filename=str(p))
output=ROOT/'dist';output.mkdir(exist_ok=True)
archive=output/('rea_pencil_for_blender-'+version+'.zip')
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
    for p in sorted((ROOT/'lea_line').rglob('*')):
        if p.is_file() and '__pycache__' not in p.parts:z.write(p,p.relative_to(ROOT))
digest=hashlib.sha256(archive.read_bytes()).hexdigest()
archive.with_suffix('.sha256').write_text(digest+'  '+archive.name+'\n',encoding='utf-8')
print(json.dumps({'path':str(archive),'version':version,'sha256':digest},indent=2))
