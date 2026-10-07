# 第三方组件与许可清单

Klado 自研代码以 Apache License 2.0 发布，全文见根目录 `LICENSE`；署名见 `NOTICE`。
第三方组件保留其自己的许可证，Apache-2.0 不会替代第三方条款。

## 核对与分发边界

- Python：在干净交付镜像中枚举全部已安装发行包，包括传递依赖、启用的 extras 和安装工具。下面的清单包含名称、实际版本与元数据许可证。
- `api/constraints.txt` 固定本次审计的版本；平台条件仍由 requirements 与包元数据计算。缺失的有效依赖、版本不符、未知许可及不符合当前发布政策的许可都会使检查失败。
- 前端：保留原 bundle 的通知，并在 `third_party/frontend/` 附带对应官方发行包与其运行时依赖的完整许可材料；`manifest.json` 记录来源、版本与文件哈希。补充依赖清单不是每个 bundle 编译输入的精确重建。
- Rust 与嵌入式 OpenSSL：`third_party/native/` 补充 5 个 wheel 对应源码 Cargo.lock 图的完整通知（含 build / optional / platform 组件），明确多许可选项的选择，并提供补充图中 colored 3.1.1（MPL-2.0）的完整源码；不声称图中每个构件都被链接。
- 原生与系统组件：`third_party/runtime/` 记录实际镜像的 Python / Debian 清单及随包通知，包含 PDFium BUILD_LICENSES；原安装位置的通知也保留；CPython 解释器全文许可、实际版本及对应源码 URL 也单独记录。Docker 构建重新生成该目录，架构与基础镜像可能使系统清单不同。
- 独立技能包：`agent.zip` 带 Klado 的 LICENSE 与技能专用 NOTICE；其示例为自研内容，不把整个应用的第三方库打入 ZIP。

## 发布政策

| 类别 | 当前策略 |
|---|---|
| MIT / BSD / ISC / Apache / PSF 等宽松许可 | 保留完整许可、版权与适用通知；遵守具体条款及商标限制。 |
| LGPL / MPL 等弱 copyleft | 单独记录，提供对应源码与许可证，保留修改、替换及重新链接权利；不得仅凭“动态导入”认定义务已免除。 |
| GPL / AGPL / SSPL / CDDL / EPL / EUPL 及受限 source-available | 当前 Python 发布政策阻断，需单独评审或替换。此分类是项目政策，不是所有这些许可证一概无法与 Apache 项目组合的法律结论。 |
| 未识别 | 检查失败，确认后再发布。 |
| SIL OFL 字体 | 保留版权和 OFL；衍生字体遵守保留字体名与原许可证要求。 |

系统镜像有独立的 copyleft 软件；上述 Python 分类不能描述整个 Linux 镜像。
系统对应源码入口与版本在 `third_party/runtime/manifest.json`，通知在该目录与 `/usr/share/doc`。

## 需特别保留的组件条款

| 组件 | 当前版本 / 许可 | 分发材料与使用方式 |
|---|---|---|
| psycopg2-binary | 2.9.9，LGPL-3.0-or-later，加 OpenSSL exception | `third_party/sources/` 提供对应源码发行包；原包 LICENSE 和 LGPL/GPL 全文保留。OpenSSL 例外不免除其他 LGPL 义务。从同版本源码编译并链接系统 libpq，不分发 wheel 捆绑的额外原生库。Klado 不修改该模块、不限制替换；可由接收方重建模块、替换 wheel，或使用修改的 Dockerfile 安装自己的版本。 |
| certifi | 2026.7.22，MPL-2.0 | `third_party/sources/` 提供对应源码发行包；许可证与版权保留。当前使用未修改源码，修改后的 MPL 覆盖文件仍须按 MPL 发布。 |
| pypdfium2 / PDFium | 5.14.0，绑定 BSD-3-Clause OR Apache-2.0；PDFium 及其构件各有许可 | 保留 wheel 中所有许可与 BUILD_LICENSES，不只登记 Python 绑定许可；官方说明：https://pypdfium2.readthedocs.io/en/stable/readme.html#licensing 。 |
| Pillow | 12.3.0，MIT-CMU 及构件各自许可 | 保留版权与许可通知，不能描述为“无需署名”；wheel 内构件许可亦保留。 |
| matplotlib | 3.11.2，PSF 派生许可及构件许可 | 保留通知，不使用名称为 Klado 背书。 |

源码发行包的官方下载来源及 SHA-256 见 `third_party/sources/manifest.json`。
源码用于行使第三方许可赋予的修改与替换权利，Klado 没有对这些权利施加额外限制。

## 前端组件

| 组件 | 版本 | 许可 | 许可材料 |
|---|---|---|---|
| Bootstrap | 5.3.0 | MIT | `third_party/frontend/bootstrap/` |
| Bootstrap Icons | 1.11.1 | MIT | `third_party/frontend/bootstrap-icons/` |
| Tabler | 1.0.0 | MIT | `third_party/frontend/@tabler/core/` |
| Chart.js | 4.4.0 | MIT | `third_party/frontend/chart.js/` |
| chartjs-plugin-datalabels | 2.2.0 | MIT | `third_party/frontend/chartjs-plugin-datalabels/` |
| html2canvas | 1.4.1 | MIT | `third_party/frontend/html2canvas/` |
| jsPDF | 2.5.1 | MIT | `third_party/frontend/jspdf/`，bundle 内通知同时保留 |
| marked | 15.0.12 | MIT | `third_party/frontend/marked/` |
| Mermaid | 11.14.0 | MIT | `third_party/frontend/mermaid/` 及依赖补充通知 |
| Lucide | 1.14.0 | ISC / Feather 衍生图标 MIT | `frontend/out/vendor/lucide/LICENSE` |
| xlsx-js-style | 1.2.0 | Apache-2.0 | `third_party/frontend/xlsx-js-style/`；原 bundle 标为 1.2.0-beta |
| Remix Icon | 4.6.0 | Apache-2.0 | `third_party/frontend/remixicon/` |
| Tabler Icons | 3.48.0 | MIT | `frontend/out/vendor/tabler-icons/LICENSE-Tabler-Icons.txt`；子集字体，修改过程见 `scripts/build_tabler_subset.py` |

自研 annotations、report-deck、report-filters、event-page 等文件采用 Klado 根许可证。
第三方文本按官方发行包原文保留；以各文件中的版权声明为准，不用人工改写的年份替代原文。

## 字体

| 字体 | 许可 | 许可文件 |
|---|---|---|
| Roboto Condensed | Apache-2.0；字体内部 name table 也记录该许可 | `frontend/out/vendor/fonts/LICENSE-Roboto-Condensed.txt` |
| Montserrat | SIL OFL 1.1 | `frontend/out/vendor/fonts/LICENSE-Montserrat.txt` |
| CoolSans SC Narrow | SIL OFL 1.1，衍生自 Noto Sans SC | `frontend/out/vendor/fonts/LICENSE-CoolSans-SC-Narrow.txt` |
| Bootstrap Icons 字体 | MIT | `third_party/frontend/bootstrap-icons/` |
| Remix Icon 字体 | Apache-2.0 | `third_party/frontend/remixicon/` |
| Tabler Icons 子集字体 | MIT | `frontend/out/vendor/tabler-icons/LICENSE-Tabler-Icons.txt` |

CoolSans SC Narrow 的字形横向缩放至 88%，已改名且保留衍生关系说明，继续以 OFL 分发。
Montserrat 字体内部较早的版权行也原样保留，与所附完整 OFL 通知一起分发。

## 已安装 Python 发行包

在交付镜像内运行 `python scripts/audit_licenses.py --write` 生成。
开发虚拟环境可能有额外工具，不用于生成交付清单；`--declared-only` 只用于检查有效依赖图。

<!-- BEGIN GENERATED INVENTORY -->
<!-- 生成于 2026-10-07，由 scripts/audit_licenses.py 生成 -->
共 **97** 个发行包。

### 🔴 阻断 —— 出现在这里即不可发布（0）

_（无）_

### 🟡 弱 copyleft —— 保留许可、署名并履行源码与替换义务（2）

`certifi`, `psycopg2-binary`

### ⚪ 未识别 —— 必须人工确认后再发布（0）

_（无）_

### 🟢 宽松（95）

`aliyun-python-sdk-core`, `aliyun-python-sdk-kms`, `annotated-types`, `anyio`, `apscheduler`, `asyncpg`, `backoff`, `bcrypt`, `beautifulsoup4`, `boto3`, `botocore`, `brotli`, `cffi`, `charset-normalizer`, `click`, `contourpy`, `crcmod`, `cryptography`, `cycler`, `defusedxml`, `ecdsa`, `et-xmlfile`, `fastapi`, `flatbuffers`, `fonttools`, `greenlet`, `h11`, `httpcore`, `httpcore2`, `httptools`, `httpx`, `httpx2`, `idna`, `jiter`, `jmespath`, `json-repair`, `kiwisolver`, `langfuse`, `lxml`, `magika`, `markdownify`, `markitdown`, `matplotlib`, `numpy`, `onnxruntime`, `openai`, `openpyxl`, `oss2`, `packaging`, `pandas`, `passlib`, `pgvector`, `pillow`, `pip`, `playwright`, `protobuf`, `pyasn1`, `pycparser`, `pycryptodome`, `pydantic`, `pydantic-core`, `pydantic-settings`, `pyee`, `pyparsing`, `pypdf`, `pypdfium2`, `python-dateutil`, `python-docx`, `python-dotenv`, `python-jose`, `python-multipart`, `python-pptx`, `pytz`, `pyyaml`, `requests`, `rsa`, `s3transfer`, `six`, `sniffio`, `soupsieve`, `sqlalchemy`, `starlette`, `truststore`, `typing-extensions`, `typing-inspection`, `tzdata`, `tzlocal`, `urllib3`, `uvicorn`, `uvloop`, `watchfiles`, `websockets`, `wrapt`, `xlsxwriter`, `zopfli`

| distribution | version | licence |
|---|---|---|
| `aliyun-python-sdk-core` | 2.16.1 | Apache Software License |
| `aliyun-python-sdk-kms` | 2.16.5 | Apache Software License |
| `annotated-types` | 0.8.0 | MIT |
| `anyio` | 4.15.1 | MIT |
| `apscheduler` | 3.11.3 | MIT License |
| `asyncpg` | 0.29.0 | Apache Software License |
| `backoff` | 2.2.1 | MIT License |
| `bcrypt` | 5.0.0 | Apache Software License |
| `beautifulsoup4` | 4.15.0 | MIT License |
| `boto3` | 1.43.106 | Apache-2.0 |
| `botocore` | 1.43.108 | Apache-2.0 |
| `brotli` | 1.2.0 | MIT |
| `certifi` | 2026.7.22 | Mozilla Public License 2.0 (MPL 2.0) |
| `cffi` | 2.1.1 | MIT-0 |
| `charset-normalizer` | 3.5.2 | MIT |
| `click` | 8.5.0 | BSD-3-Clause |
| `contourpy` | 1.4.0 | BSD-3-Clause |
| `crcmod` | 1.7 | MIT License |
| `cryptography` | 46.0.3 | Apache-2.0 OR BSD-3-Clause |
| `cycler` | 0.12.1 | BSD License |
| `defusedxml` | 0.7.1 | Python Software Foundation License |
| `ecdsa` | 0.19.2 | MIT |
| `et-xmlfile` | 2.0.0 | MIT License |
| `fastapi` | 0.115.0 | MIT License |
| `flatbuffers` | 25.12.19 | Apache Software License |
| `fonttools` | 4.66.1 | MIT |
| `greenlet` | 3.5.6 | MIT AND PSF-2.0 |
| `h11` | 0.16.0 | MIT License |
| `httpcore` | 1.0.9 | BSD-3-Clause |
| `httpcore2` | 2.13.1 | BSD-3-Clause |
| `httptools` | 0.8.0 | MIT |
| `httpx` | 0.27.2 | BSD-3-Clause |
| `httpx2` | 2.13.1 | BSD-3-Clause |
| `idna` | 3.20 | BSD-3-Clause |
| `jiter` | 0.17.0 | MIT |
| `jmespath` | 1.1.0 | MIT License |
| `json-repair` | 0.63.5 | MIT |
| `kiwisolver` | 1.5.1 | BSD License |
| `langfuse` | 2.60.10 | MIT License |
| `lxml` | 6.1.3 | BSD-3-Clause |
| `magika` | 0.6.3 | Apache Software License |
| `markdownify` | 1.2.3 | MIT License |
| `markitdown` | 0.1.8 | MIT |
| `matplotlib` | 3.11.2 | Python Software Foundation License |
| `numpy` | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| `onnxruntime` | 1.30.0 | MIT License |
| `openai` | 3.26.0 | Apache-2.0 |
| `openpyxl` | 3.1.5 | MIT License |
| `oss2` | 2.19.1 | MIT License |
| `packaging` | 24.2 | Apache Software License; BSD License |
| `pandas` | 2.2.3 | BSD License |
| `passlib` | 1.7.4 | BSD |
| `pgvector` | 0.3.2 | MIT |
| `pillow` | 12.3.0 | MIT-CMU |
| `pip` | 25.0.1 | MIT License |
| `playwright` | 1.63.0 | Apache-2.0 |
| `protobuf` | 7.36.2 | 3-Clause BSD License |
| `psycopg2-binary` | 2.9.9 | GNU Library or Lesser General Public License (LGPL) |
| `pyasn1` | 0.6.4 | BSD-2-Clause |
| `pycparser` | 3.0 | BSD-3-Clause |
| `pycryptodome` | 3.24.0 | BSD License; Public Domain |
| `pydantic` | 2.13.5 | MIT |
| `pydantic-core` | 2.46.5 | MIT |
| `pydantic-settings` | 2.5.2 | MIT |
| `pyee` | 13.0.1 | MIT License |
| `pyparsing` | 3.3.3 | MIT |
| `pypdf` | 6.19.0 | BSD-3-Clause |
| `pypdfium2` | 5.14.0 | BSD-3-Clause, Apache-2.0, dependency licenses |
| `python-dateutil` | 2.9.0.post0 | Apache Software License; BSD License |
| `python-docx` | 1.2.0 | MIT License |
| `python-dotenv` | 1.2.3 | BSD-3-Clause |
| `python-jose` | 3.3.0 | MIT License |
| `python-multipart` | 0.0.12 | Apache-2.0 |
| `python-pptx` | 1.0.2 | MIT License |
| `pytz` | 2026.5 | MIT License |
| `pyyaml` | 6.0.3 | MIT License |
| `requests` | 2.34.2 | Apache Software License |
| `rsa` | 4.9.1 | Apache Software License |
| `s3transfer` | 0.19.2 | Apache Software License |
| `six` | 1.17.0 | MIT License |
| `sniffio` | 1.3.1 | Apache Software License; MIT License |
| `soupsieve` | 2.10 | MIT |
| `sqlalchemy` | 2.0.35 | MIT License |
| `starlette` | 0.38.6 | BSD-3-Clause |
| `truststore` | 0.10.4 | MIT |
| `typing-extensions` | 4.16.0 | PSF-2.0 |
| `typing-inspection` | 0.4.4 | MIT |
| `tzdata` | 2026.5 | Apache-2.0 |
| `tzlocal` | 5.4.4 | MIT |
| `urllib3` | 2.8.0 | MIT |
| `uvicorn` | 0.30.6 | BSD-3-Clause |
| `uvloop` | 0.23.0 | Apache Software License; MIT License |
| `watchfiles` | 1.3.0 | MIT License |
| `websockets` | 17.2 | BSD-3-Clause |
| `wrapt` | 1.17.3 | BSD License |
| `xlsxwriter` | 3.2.9 | BSD License |
| `zopfli` | 0.4.3 | Apache Software License |
<!-- END GENERATED INVENTORY -->

## 变更验证

1. 更新依赖约束后，在干净镜像运行完整许可审计与单元测试。
2. 重新生成运行时通知与清单；弱 copyleft 版本改变时同步对应源码发行包与哈希。
3. 变更第三方前端资源时同步官方完整许可材料与 manifest；保留 bundle 自带通知。
4. 修改技能源后运行 `bash scripts/build_agent_skill.sh`，检查全部文件、CRC、许可证与版本。
5. 公开发布通过快照导出及隐私检查，检查所有公开引用的历史，而不只检查当前 HEAD。
