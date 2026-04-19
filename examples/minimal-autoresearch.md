# Example Autoresearch Mission

Build a small persistent research runtime around Codex.

## Goal

Create a loop that can:

1. Read this mission file.
2. Decide the next useful step.
3. Persist progress locally.
4. Sleep while waiting on a long-running task.
5. Resume later without losing context.

## Constraints

1. Keep all generated files under the chosen runtime directory.
2. Prefer local state over chat memory.
3. Emit concise progress updates suitable for appending to a Lark document.
