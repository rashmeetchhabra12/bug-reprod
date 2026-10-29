# Otter Wiki Preview evaluation

On 2026-10-29, a bounded Codex CLI investigation tested the workflow associated
with [Otter Wiki issue #243](https://github.com/redimp/otterwiki/issues/243):
create a page named `foobarß` and inspect its rendered title in Preview before
saving. The frozen expectation was `heading_text`, level 1, exact `Foobarß`.
The agent received the task and browser observations, including button hints;
it was not supplied a route, selectors, source code or a previous recipe.

| Run | Formal check | Actions | Decision calls |
| --- | --- | --- | --- |
| Codex investigation, published 2.10.5 image | failed | 6 | 7 |
| Separate 2.10.5 instance, recorded replay | failed | 6 | 0 |
| Separate 2.10.6 instance, recorded replay | passed | 6 | 0 |

All three runs had zero action failures and zero execution errors. The two
2.10.5 runs showed the same corrupted level-1 heading in Preview, with identical
Preview screenshots. The 2.10.6 replay showed `Foobarß`. The published 2.10.5
image reported runtime version `v2.10.5-1-g9020f49`; 2.10.6 reported
`v2.10.6-0-g7872045`.

Both replays consumed the exported recipe unchanged; all three saved recipe
files were byte-identical, with raw-file SHA-256
`db0636de79deed372f82a4440c4e1ea4e69e886608e01b7c87cf8401885eadc9`.
The frozen expectation fingerprint also matched across all three runs.

This result covers an unsaved page in Preview. Saving and persistence were not
tested, and a passing replay is not a general correctness claim. The strict
external-request policy remained enabled; results describe that restricted
environment. Generated evidence and local audit files are not tracked here.
