# Dependency and data attribution

Reviewed against `pyproject.toml`, `uv.lock`, installed distribution metadata,
tracked source and the synthetic corpus manifests. This records actual dependencies
and provenance.

## Project and original data

ShopfloorPilot's original code, scenarios and synthetic knowledge notes are released
under the [MIT License](../../LICENSE). Dependency licenses below describe those
packages, not ownership of this repository.

| Resource | Provenance and use |
| --- | --- |
| [Catalog and scenarios](../../scenarios/manifest.json) | Project-authored synthetic machines, process routes, orders, telemetry and interventions. Manifest hashes bind source files. There is no proprietary manufacturing dataset. |
| [Maintenance corpus](../../data/knowledge/manifest.json) | Eleven project-authored synthetic JSON notes. Each source declares `original_synthetic` and its provenance detail. The manifest pins source hashes; notes contain no copied equipment manual text. |
| [Agent request fixtures](../../data/agent_requests/requests.json) | Local synthetic English/Chinese request cases and expected supported interpretations. |
| [Retrieval queries](../../tests/retrieval/fixtures/frozen_queries.json) and test fixtures | Local synthetic test/evaluation material, including known-answer schedules and truth-isolation/adversarial cases. |
| Evaluation CSV and browser screenshots | Generated from this application's synthetic runs. Selected sanitized evidence is tracked; runtime databases and complete traces are excluded. |

The historical roadmap mentions LangGraph, JobShopLib and AI4I as possible references.
They are not installed direct dependencies, imported implementation components or
bundled datasets in this delivery. Their mention is not a reuse or license claim.
No model weights, external image assets or external maintenance corpus are bundled.

## Direct dependencies

Versions and license identifiers below were read from the installed locked environment
with `importlib.metadata`.
Upstream project links identify the packages; installed metadata/license files are
the evidence for these specific versions.

| Dependency | Locked/installed version | Role | Declared license | Upstream |
| --- | --- | --- | --- | --- |
| SimPy | 4.1.2 | Discrete-event simulation | MIT | [SimPy](https://gitlab.com/team-simpy/simpy/) |
| OR-Tools | 9.15.6755 | CP-SAT scheduling | Apache-2.0 | [OR-Tools](https://github.com/google/or-tools) |
| Pydantic | 2.14.1 | Typed local contracts and validation | MIT | [Pydantic](https://github.com/pydantic/pydantic) |
| Streamlit | 1.66.0 | Local dashboard | Apache-2.0 | [Streamlit](https://github.com/streamlit/streamlit) |
| Plotly | 6.9.0 | Gantt/telemetry charts | MIT | [Plotly.py](https://github.com/plotly/plotly.py) |
| OpenAI Python SDK | 3.35.1 | Compatible HTTP client to DeepSeek only | Apache-2.0 | [openai-python](https://github.com/openai/openai-python) |
| jsonschema | 4.26.0 | Local provider tool-schema validation | MIT | [jsonschema](https://github.com/python-jsonschema/jsonschema) |
| pytest (development) | 8.4.2 | Automated verification | MIT | [pytest](https://github.com/pytest-dev/pytest) |
| Ruff (development) | 0.17.1 | Lint and formatting | MIT | [Ruff](https://github.com/astral-sh/ruff) |

License texts were present under the respective distribution's
`*.dist-info/licenses/` for every listed package except Streamlit. Streamlit's installed
metadata declares `License-Expression: Apache-2.0` but its wheel's recorded file list
contains no license file. OpenAI's installed package also includes a vendored
`httpx_aiohttp/LICENSE`; Plotly ships its own third-party JavaScript notices. Those
package-provided notices remain with the installed dependencies; this delivery does
not copy or strip them.

The build backend is Hatchling (`>=1.26,<2` in `pyproject.toml`), used in uv's isolated
build environment, not installed as a runtime distribution. The local uv cache contains
Hatchling 1.32.4 with `License-Expression: MIT` and its license text; the
[Hatch project](https://github.com/pypa/hatch) is its upstream source. Cache presence
does not prove which version a future isolated build will select within that range.
The exact build dependency should be retained if an environment image or binary
distribution is packaged later.

The tested runtime is CPython 3.12.15 (PSF license family, with bundled component
notices) and stdlib SQLite 3.53.1 (public domain). The setup tool is uv 0.13.4
(MIT OR Apache-2.0). These tool/runtime attributions are not application Python dependencies. This source-only delivery does not redistribute
the interpreter, uv, dependency wheels or a browser binary.

## Vendored browser library

| Library | Version | Role | License | Upstream |
| --- | --- | --- | --- | --- |
| three.js (`three.module.js`, `three.core.js`, `examples/jsm/controls/OrbitControls.js`, `examples/jsm/environments/RoomEnvironment.js`, `examples/jsm/geometries/RoundedBoxGeometry.js`, `examples/jsm/utils/BufferGeometryUtils.js`) | 0.186.1 | 3D rendering of the factory twin view | MIT | [three.js](https://github.com/mrdoob/three.js) |

The files are byte-for-byte copies from the npm package tarball, whose SHA-512
matched the registry integrity value. They are tracked with the unchanged upstream
`LICENSE` and a [provenance record](../../src/shopfloor_pilot/ui/factory_twin_frontend/vendor/three/PROVENANCE.md)
listing per-file SHA-256 hashes, which `tests/ui/test_factory_twin.py` pins.
`.gitattributes` exempts this directory from whitespace/EOL normalization so the
bytes stay identical. All scene geometry, the floor texture (drawn on a canvas at runtime) and the
image-based lighting (`RoomEnvironment`, generated procedurally) are created in the
browser; no external models, textures, HDR maps or fonts are bundled.

Transitive dependencies remain pinned in [uv.lock](../../uv.lock). This is a direct
dependency/data attribution record, not a complete transitive SBOM or legal review.
No project copyright or dependency notice is removed by the packaging workflow.
