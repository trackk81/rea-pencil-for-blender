import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('core', ROOT / 'lea_line/update_core.py')
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

class Updates(unittest.TestCase):
    def setUp(self):
        self.assertTrue(core.process_alive(os.getpid()))
        self.alive = patch.object(core, 'process_alive', return_value=False)
        self.alive.start()
        self.addCleanup(self.alive.stop)
        self.others = patch.object(core, 'other_blender_running', return_value=False)
        self.others.start()
        self.addCleanup(self.others.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.zip = self.root / 'new.zip'
        with zipfile.ZipFile(self.zip, 'w') as z:
            z.writestr('lea_line/__init__.py', 'new = True\n')
            z.writestr('lea_line/release.json', json.dumps({'product':'rea-pencil-for-blender','version':[0,28,7]}))
        self.release = {'version':(0,28,7),'sha256':hashlib.sha256(self.zip.read_bytes()).hexdigest()}
        self.target = self.root / 'addons/lea_line'
        self.target.mkdir(parents=True)
        (self.target / '__init__.py').write_text('old = True\n')
        self.config = self.root / 'config'

    def tearDown(self): self.tmp.cleanup()

    def test_numeric_versions_and_product_filter(self):
        base={'tag_name':'v0.28.7','assets':[{'name':'rea_pencil_for_blender-0.28.7.zip','browser_download_url':'https://github.com/'+core.REPOSITORY+'/releases/download/v0.28.7/rea_pencil_for_blender-0.28.7.zip','size':42,'digest':'sha256:'+'a'*64}]}
        wrong={'tag_name':'v9.0.0','assets':[{'name':'Maya.zip'}]}
        self.assertEqual(core.select_release([wrong,base],(0,28,6))['version'],(0,28,7))
        self.assertIsNone(core.select_release([dict(base,prerelease=True)],(0,28,6)))
        self.assertIsNone(core.select_release([base],(0,28,7)))

    def test_same_process_stage_never_overwrites_loaded_dlls(self):
        core.stage(self.zip,self.release,self.config,self.target,pid=10)
        self.assertFalse(core.apply_pending(self.config,self.target,pid=10))
        self.assertIn('old', (self.target/'__init__.py').read_text())

    def test_cold_start_swap_preserves_backup(self):
        core.stage(self.zip,self.release,self.config,self.target,pid=10)
        self.assertTrue(core.apply_pending(self.config,self.target,pid=11))
        self.assertIn('new',(self.target/'__init__.py').read_text())
        marker=core.pending(self.config)
        self.assertIn('old',(Path(marker['backup'])/'__init__.py').read_text())
        self.assertFalse(core.apply_pending(self.config,self.target,pid=12))

    def test_checksum_failure_keeps_installed_version(self):
        core.stage(self.zip,self.release,self.config,self.target,pid=10)
        self.zip.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):core.apply_pending(self.config,self.target,pid=11)
        self.assertIn('old',(self.target/'__init__.py').read_text())

    def test_traversal_wrong_product_and_symlinks_rejected(self):
        for name in ('../escape','lea_line/../escape','/lea_line/escape','lea_line\\escape'):
            with zipfile.ZipFile(self.zip,'w') as z:z.writestr(name,'bad')
            with self.assertRaises(ValueError):core.validate_zip(self.zip,(0,28,7))
        with zipfile.ZipFile(self.zip,'w') as z:
            z.writestr('lea_line/release.json',json.dumps({'product':'maya','version':[0,28,7]}))
            z.writestr('lea_line/__init__.py','')
        with self.assertRaises(ValueError):core.validate_zip(self.zip,(0,28,7))
        with zipfile.ZipFile(self.zip,'w') as z:
            link=zipfile.ZipInfo('lea_line/link');link.external_attr=(0o120777<<16);z.writestr(link,'../escape')
        with self.assertRaises(ValueError):core.validate_zip(self.zip,(0,28,7))

    def test_failed_commit_rolls_back_directory(self):
        core.stage(self.zip,self.release,self.config,self.target,pid=10)
        with patch.object(core,'write_json',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):core.apply_pending(self.config,self.target,pid=11)
        self.assertIn('old',(self.target/'__init__.py').read_text())

    def test_target_mismatch_ignored(self):
        core.stage(self.zip,self.release,self.config,self.target,pid=10)
        self.assertFalse(core.apply_pending(self.config,self.root/'other',pid=11))

    def test_other_process_waits_for_origin_to_exit(self):
        core.stage(self.zip,self.release,self.config,self.target,pid=10)
        with patch.object(core,'process_alive',return_value=True):
            self.assertFalse(core.apply_pending(self.config,self.target,pid=11))
        self.assertIn('old',(self.target/'__init__.py').read_text())

    def test_unrelated_blender_also_defers_install(self):
        core.stage(self.zip,self.release,self.config,self.target,pid=10)
        with patch.object(core,'other_blender_running',return_value=True):
            self.assertFalse(core.apply_pending(self.config,self.target,pid=11))
        self.assertIn('old',(self.target/'__init__.py').read_text())

if __name__=='__main__':unittest.main()
