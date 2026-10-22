# The rules of the architect

These rules are added to what the architect of every swarm is told. Change them to change how it writes wheels.

- Prefer a few general wheels to many specific ones.
- A wheel that runs commands gives back only what the agent needs next, never a whole log.
- Write the code of a wheel so that a person can read it: short functions, clear names, and an error message that says what went wrong and what to do.
