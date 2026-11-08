0.28.11 — GPU 뷰포트 미리보기와 곡선·점선·맵·숨은 선

- 새 장면의 Viewport Preview 기본 계산 방식을 GPU로 설정합니다. 기존 파일에 저장된 Native 선택은 유지합니다.
- Solid 모드에서 시점을 이동하는 동안에도 선을 표시합니다. GPU 미리보기에는 카메라나 Follow Viewport 버튼이 필요하지 않으며, 장면 렌더 카메라를 변경하지 않습니다.
- 굵기·알파 곡선과 거리별 감소 곡선을 지원합니다. 굵기와 알파에 독립적으로 연결한 Reduction Settings 및 기준 오브젝트도 반영합니다.
- 연결된 선의 화면상 길이를 GPU에서 계산하여 점선과 위상을 이어 줍니다. 곡선 편집 후 미리보기를 자동 갱신합니다.
- Color Map / Size Map의 Screen·Object UV·Object Color 속성을 지원합니다.
- 캐시한 메시의 GPU 깊이 패스로 보이는 선과 숨은 선을 구분합니다.
- 지원하지 않는 브러시·맵·선 설정은 Native 경로로 처리합니다. Native Follow Viewport에는 시점 이동이 멈춘 뒤 갱신하는 방식도 포함됩니다.

## 사용 방법

1. Blender 4.4 → Edit → Preferences → Add-ons → Install from Disk에서 `rea_pencil_for_blender-0.28.11.zip`을 설치하고 활성화합니다. GitHub Source code ZIP은 설치용이 아닙니다.
2. N 패널 → REA Pencil Line → Preview & Render → Viewport Preview를 켭니다. 3D 뷰는 Solid, X-Ray 끄기 상태로 사용합니다.
3. 기존 파일은 뷰포트 계산을 GPU로 선택합니다. Brush Detail의 굵기·알파 곡선, 점선, Brush Settings의 Color/Size Map 및 Line Set의 Visible/Hidden 설정을 사용합니다.
4. 기존 버전에서는 업데이트 확인 → 새 버전 다운로드 후 작업을 저장하고 Blender를 완전히 종료·재실행하면 적용됩니다. 여러 Blender가 실행 중이면 모두 종료해야 합니다. 이전 설치 폴더는 백업됩니다.

## 검증 범위와 남은 차이

- RTX 5090 / Blender 4.4에서 GPU 픽셀 검사 28개, 실제 설치·장면 검사 16개, 업데이트 회귀 검사 9개를 통과했습니다. 정식 버전 ZIP 설치·버전 정보도 별도 확인합니다.
- 주방 장면의 곡선·점선·보이는 선/숨은 선을 켠 확대 시점 이동에서 화면 갱신 중앙값은 약 15.5ms였습니다. 초기 메시 준비는 약 2.2초이며, 설정 변경 시 메시 재준비가 남아 있습니다. 장면·하드웨어·화면 크기에 따라 달라집니다.
- GPU는 Solid / X-Ray 끄기 / 단일 View Layer 범위입니다. 깊이 패스는 메시를 대상으로 합니다.
- Object UV 또는 Object Color와 숨은 선의 동시 사용은 아직 지원하지 않습니다. 숨은 선에는 Screen 맵을 사용하세요. 왜곡, 다중 스탬프·브러시 맵·랜덤, Line Functions·재질 마스크, 일부 특수선·오프셋·캡은 Native가 필요합니다.
- 분기점에서 선을 나누며, 가려진 구간에서 점선 위상을 재시작하지 않습니다. 루프 시작점·점선 이음새·캡·연결부·AA·반투명 중첩은 Native 및 원본 Pencil+와 차이가 남아 있습니다. 이미지 필터·랩, 애니메이션·인스턴스·여러 뷰 레이어의 광범위한 호환성은 검증 중입니다.

최종 렌더를 GPU 미리보기와 같은 경로로 바꾸는 릴리스가 아닙니다. Rust DLL은 이전 배포와 같습니다. 원본 Pencil+와 기능·화질·속도 동등성을 주장하지 않습니다.
