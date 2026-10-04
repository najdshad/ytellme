# Prompt changelog

Each agent's system prompt is versioned as `prompts/<agent>.v<N>.md`. The version
that produced an artifact is recorded in `runs/<slug>/run.log`, so two test runs
are comparable and a regression can be traced to a prompt diff.

Bump the version, edit the copy, never the file that a past run used.

## researcher.v1

- Initial version. Expansive dossier, explicitly no pre-digesting.

## editor.v1

- Initial version. One angle, required cut list, required beat structure.

## scriptwriter.v1

- Initial version. Style block defaults to thin-line stick figures on off-white
  paper; negative prompt limited to failure modes.