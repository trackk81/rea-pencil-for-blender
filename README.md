# rea pencil for blender

Blender 4.4용 독립 라인 렌더링 애드온입니다. Maya용 ReaPencil과 별도 제품입니다.

## 설치

1. [Releases](https://github.com/trackk81/rea-pencil-for-blender/releases)에서 `rea_pencil_for_blender-버전.zip`을 받습니다. GitHub의 Source code ZIP은 설치용이 아닙니다.
2. Blender → Edit → Preferences → Add-ons → Install from Disk에서 ZIP을 선택하고 활성화합니다.
3. 3D View의 N 패널 → REA Pencil Line → 라인 편집기 열기로 시작합니다.

## 업데이트

REA Pencil Line 패널 또는 애드온 환경설정에서 **업데이트 확인 → 새 버전 다운로드**를 누릅니다. 작업을 저장하고 Blender를 종료한 뒤 다시 실행하면 적용됩니다. 자동 종료·강제 재시작은 하지 않습니다.

공식 공개 GitHub Release의 제품명·버전·SHA-256·ZIP 경로·최소 Blender 버전을 확인합니다. 시험판은 자동 선택하지 않습니다. 사용 중인 DLL을 덮어쓰지 않고 다음 프로세스 시작 시 설치 폴더를 교체하며, 이전 폴더를 같은 addons 디렉터리의 `.lea-line-backup-*`에 보존합니다. 업데이트 준비 중 인터넷 오류가 나면 현재 설치를 유지합니다.

처음 업데이트 기능을 사용하는 0.28.5 사용자는 0.28.6 ZIP을 한 번 수동 설치해야 합니다. 그 이후 버전부터 버튼으로 확인·준비할 수 있습니다.

## 0.28.6

- GitHub 버전 확인·업데이트 다운로드 버튼 추가.
- 재시작 적용, 체크섬 검증, 이전 버전 백업 및 교체 실패 시 복구.
- 렌더링 계산·Rust DLL은 0.28.5와 동일합니다. 실험 중인 0.29.0 AA 경로는 포함하지 않습니다.

## 알려진 제한

원본 Pencil+와 기능·UI·화질·속도가 완전히 같다는 의미가 아닙니다. Normal 스탬프의 최종 렌더에서는 반투명 중첩 때문에 미리보기보다 선이 진해질 수 있습니다. AA·브러시·숨은 선·복잡한 인스턴스 등의 검증도 계속 진행 중입니다.

## 다음 버전 배포

`lea_line/__init__.py`의 `bl_info`와 `lea_line/release.json`을 같은 버전으로 변경하고, `python -m unittest discover -s tests` 및 Blender 검증 후 `python tools/package.py`로 설치 ZIP을 만듭니다. GitHub에 `v버전` Release를 생성해 생성된 ZIP과 SHA-256 파일을 첨부합니다. 소스 태그만 만들면 업데이트로 인식되지 않습니다.

업데이트 버튼은 공개된 정식 Release 중 제품 ZIP과 GitHub SHA-256 digest가 있는 가장 높은 버전을 선택합니다. 소스 코드와 개발 테스트·역설계 자료는 별도로 관리합니다.
