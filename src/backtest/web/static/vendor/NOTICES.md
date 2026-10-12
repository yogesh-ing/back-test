# Local UI dependencies

These files are served locally so the trading workspace does not depend on a font or chart CDN. No runtime npm/build step is required.

| Dependency | Pinned package | Files | License |
| --- | --- | --- | --- |
| Chart.js | `chart.js@4.4.1` (the existing app version) | `chart.umd-4.4.1.js` | MIT — `Chartjs-LICENSE.md` |
| Inter variable | `@fontsource-variable/inter@5.3.0` | `../fonts/inter-latin-variable.woff2` | SIL Open Font License 1.1 — `../fonts/Inter-LICENSE.txt` |
| JetBrains Mono | `@fontsource/jetbrains-mono@5.3.0` | `../fonts/jetbrains-mono-latin-{400,500}.woff2` | SIL Open Font License 1.1 — `../fonts/JetBrainsMono-LICENSE.txt` |

Icon paths in `_macros.html` are simple inline geometric drawings; no icon font or remote icon service is used.

To update a dependency, obtain the pinned package, copy only the required distributable assets and license, then run the UI checks. Verify the package version in this table against the installed package when updating.
