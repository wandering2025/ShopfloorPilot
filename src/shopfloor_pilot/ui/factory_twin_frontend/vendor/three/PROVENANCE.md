# Vendored three.js

- Project: three.js — https://threejs.org/ (source https://github.com/mrdoob/three.js)
- Version: 0.186.1 (npm package `three`)
- License: MIT, copied unchanged in `LICENSE` (Copyright © 2010-2026 three.js authors)
- Obtained: 2026-10-22 from `https://registry.npmjs.org/three/-/three-0.186.1.tgz`
- Tarball integrity (npm registry `dist.integrity`, verified locally before copying):
  `sha512-blFeqb49wRCSGUGj7gtpfnSGHy2lwDk94RhUmS1c/hTby70kvChbWpkJ4Pm1390LqzzvTmzgXKHPEafJwCb8jA==`

Files are byte-for-byte copies from the package (no edits, no minification):

File | Package path | SHA-256
--- | --- | ---
`three.module.js` | `build/three.module.js` | `9052042d676cb0fdc1ddfefe193053f34b7ac0513a616fdac4535d49987812ea`
`three.core.js` | `build/three.core.js` | `9edde002b066a9a05676a6127f67735b62baf399bdea529f2f7e31657da769e6`
`OrbitControls.js` | `examples/jsm/controls/OrbitControls.js` | `3d79d07ecb686b4e5d93232eedab255331c1beef711e13164eaa1f68655a5f2b`
`RoomEnvironment.js` | `examples/jsm/environments/RoomEnvironment.js` | `55f466192cc84298755a424c5e040345006b2ee1455589b3b54126c2ea4123f4`
`RoundedBoxGeometry.js` | `examples/jsm/geometries/RoundedBoxGeometry.js` | `c5feab96123858ed8823c889b238dc8734adab7e760e3cfd21e7f3f2f3e348e3`
`BufferGeometryUtils.js` | `examples/jsm/utils/BufferGeometryUtils.js` | `9fb63427ce6641fa14fd0baff9cc4d1b5f9c3d85fd084bf2e90e803c44ec1797`
`LICENSE` | `LICENSE` | `8b378ebe60e2fe500158cb0ac71cb5e8b7d92953c2abcc63a0eb90499653b5bc`

`RoomEnvironment.js`, `RoundedBoxGeometry.js` and `BufferGeometryUtils.js` were added on 2026-10-22 (S15 r3)
from the same tarball, re-verified against the same SHA-512 before copying.
`tests/ui/test_factory_twin.py` pins these hashes. The ShopfloorPilot scene builds
all geometry procedurally and never uses the library's file/network loaders.
