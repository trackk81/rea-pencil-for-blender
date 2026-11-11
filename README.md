# rea pencil for blender

Blender 4.4·4.5용 독립 라인 렌더링 애드온입니다. Maya용 ReaPencil과 별도 제품입니다.

## 설치

1. [Releases](https://github.com/trackk81/rea-pencil-for-blender/releases)에서 `rea_pencil_for_blender-버전.zip`을 받습니다. GitHub의 Source code ZIP은 설치용이 아닙니다.
2. Blender → Edit → Preferences → Add-ons → Install from Disk에서 ZIP을 선택하고 활성화합니다.
3. 3D View의 N 패널 → REA Pencil Line → 라인 편집기 열기로 시작합니다.

## 업데이트

REA Pencil Line 패널 또는 애드온 환경설정에서 **업데이트 확인 → 새 버전 다운로드**를 누릅니다. 작업을 저장하고 Blender를 종료한 뒤 다시 실행하면 적용됩니다. 자동 종료·강제 재시작은 하지 않습니다.

공식 공개 GitHub Release의 제품명·버전·SHA-256·ZIP 경로·최소 Blender 버전을 확인합니다. 시험판은 자동 선택하지 않습니다. 사용 중인 DLL을 덮어쓰지 않고 다음 프로세스 시작 시 설치 폴더를 교체하며, 이전 폴더를 같은 addons 디렉터리의 `.lea-line-backup-*`에 보존합니다. 업데이트 준비 중 인터넷 오류가 나면 현재 설치를 유지합니다.

처음 업데이트 기능을 사용하는 0.28.5 사용자는 최신 설치 ZIP을 한 번 수동 설치해야 합니다. 그 이후 버전부터 버튼으로 확인·준비할 수 있습니다.

## 0.28.18

Blender 4.5의 미리보기 차단과 GPU → 일반 미리보기 전환 시 카메라 시점 문제를 수정했습니다. 자세한 내용은 [릴리스 노트](RELEASE_NOTES.md)를 확인하세요.

## 0.28.17

일반/GPU 미리보기의 Outline·파형 방향과 화면 배율, CPU 외곽선 판정을 개선했습니다. CPU Wave와 닫힌 파형의 원본 일치는 아직 지원 범위 밖입니다. 자세한 범위는 [릴리스 노트](RELEASE_NOTES.md)를 확인하세요.

## 0.28.13

- REA 그래프가 있는 장면에서 **F12** 또는 REA의 **Render**로 장면에 자체 GPU 선을 합성합니다. **F12 GPU 선 렌더**는 기본으로 켜져 있으며, 끄면 기존 Blender 렌더 경로를 사용합니다.
- **Preview & Render → GPU 라인만 렌더 · 투명 배경**으로 GPU 선을 별도로 출력합니다. 투명도를 보존하려면 PNG / RGBA로 저장하세요.
- 기존 Line Art 생성 객체와 REA 미리보기 객체는 중복되지 않도록 합성 렌더 중에만 제외합니다. 직접 그린 Grease Pencil은 장면에 유지합니다.
- GPU 미리보기를 Solid, 재질 미리보기, EEVEE/Cycles Rendered 뷰에서 사용할 수 있습니다.
- 지원 범위와 깊이 판정 등 남은 차이는 [릴리스 노트](RELEASE_NOTES.md)를 확인하세요.

## 0.28.12

- **Preview & Render → 라인만 렌더 · 투명 배경**으로 현재 프레임·활성 뷰 레이어의 Freestyle 선만 출력합니다. 저장할 때 PNG / RGBA를 선택하세요. 기존 Render·F12는 이전처럼 장면을 렌더합니다.
- GPU 미리보기를 재질 미리보기에서도 표시합니다. Solid·재질 미리보기에서 회전 중 자기 표면의 깊이 때문에 선이 끊기던 문제를 수정했습니다. Rendered 뷰는 아직 지원 범위가 아닙니다.
- 라인만 출력할 때 임시로 바꾼 합성 노드·출력·뷰 레이어·렌더 설정을 복구합니다. 자세한 검증 범위와 제한은 [릴리스 노트](RELEASE_NOTES.md)를 확인하세요.

## 0.28.11

- 새 장면의 뷰포트 미리보기는 기본 GPU 방식입니다. Solid / X-Ray 끄기에서 카메라 없이 자유 시점을 이동하는 동안 선을 유지합니다. 기존 파일의 Native 선택은 유지합니다.
- 굵기·알파 곡선, 독립 거리 감소·기준점, 연결된 점선·위상, Color/Size Map의 Screen·Object UV·Object Color, 메시 깊이를 이용한 숨은 선을 지원합니다.
- GPU 미리보기에는 Follow Viewport 버튼이 필요하지 않습니다. 기존 파일은 뷰포트 계산을 GPU로 선택하세요. 최종 렌더는 기존 경로를 사용합니다.
- 미지원 설정은 Native로 처리합니다. Object UV/Object Color와 숨은 선의 동시 사용은 미지원이며, AA·캡·연결부·위상은 원본과 차이가 남아 있습니다. 자세한 사용법과 검증 범위는 [릴리스 노트](RELEASE_NOTES.md)를 확인하세요.

## 0.28.10

- Preview & Render에 카메라가 없을 때 **카메라 자동 생성** 버튼을 표시합니다.
- 현재 또는 추적 중인 3D 뷰의 원근·직교 시점으로 장면 카메라를 만듭니다. 3D 뷰가 없으면 장면 메시를 향하도록 배치합니다.
- 기존 카메라, 오브젝트 선택, 렌더 해상도를 유지하며 실행 취소를 지원합니다.

## 0.28.9

- 기본 큐브 등의 미리보기 선이 모서리에서 옆으로 밀리던 문제를 수정했습니다. Line Art의 기본 0.05 월드 단위 깊이 이동을 생성 미리보기에 적용하지 않습니다.
- 재질별 미리보기의 내부 Line Art에도 같은 설정을 전달합니다. Brush Detail의 의도적인 X/Y/Normal Offset은 유지합니다.
- 최종 Freestyle 렌더의 위치나 설정은 바꾸지 않습니다.

## 0.28.8

- Blender를 여러 개 실행할 때는 다른 Blender와 업데이트를 준비한 프로세스가 모두 끝날 때까지 설치 교체를 보류합니다.
- 공개 Asset API 다운로드·재시작 적용·백업을 유지합니다.

## 0.28.7

- GitHub 공개 Asset API 다운로드 경로로 게이트웨이 오류를 피합니다.
- 0.28.6의 재시작 적용·백업·복구 기능을 유지합니다.

## 0.28.6

- GitHub 버전 확인·업데이트 다운로드 버튼 추가.
- 재시작 적용, 체크섬 검증, 이전 버전 백업 및 교체 실패 시 복구.
- 렌더링 계산·Rust DLL은 0.28.5와 동일합니다. 실험 중인 0.29.0 AA 경로는 포함하지 않습니다.

## 알려진 제한

원본 Pencil+와 기능·UI·화질·속도가 완전히 같다는 의미가 아닙니다. Normal 스탬프의 최종 렌더에서는 반투명 중첩 때문에 미리보기보다 선이 진해질 수 있습니다. AA·브러시·숨은 선·복잡한 인스턴스 등의 검증도 계속 진행 중입니다.

## 다음 버전 배포

`lea_line/__init__.py`의 `bl_info`와 `lea_line/release.json`을 같은 버전으로 변경하고, `python -m unittest discover -s tests` 및 Blender 검증 후 `python tools/package.py`로 설치 ZIP을 만듭니다. GitHub에 `v버전` Release를 생성해 생성된 ZIP과 SHA-256 파일을 첨부합니다. 소스 태그만 만들면 업데이트로 인식되지 않습니다.

업데이트 버튼은 공개된 정식 Release 중 제품 ZIP과 GitHub SHA-256 digest가 있는 가장 높은 버전을 선택합니다. 소스 코드와 개발 테스트·역설계 자료는 별도로 관리합니다.
