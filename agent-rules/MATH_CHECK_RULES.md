# No report should be generated without strictly following these rules.

## Rules for checking a mathematical text:
1) Check every theorem, lemma, proposition, corollary and claim, and the key steps of its proof, in the order they appear.
2) A step is only valid if it follows from the earlier steps, the assumptions, or a cited result. Never trust a step because it looks plausible.
3) "Clearly", "obviously", "it is easy to see" and "by a similar argument" are not justifications. If the step is not easy to see, report a GAP.
4) Check that every assumption is stated, and that the hypotheses of every result that is used (also the cited ones) are satisfied.
5) Check the quantifiers and their order, the edge cases and the degenerate cases.
6) Check that every exchange of limits, sums and integrals, and every use of compactness, continuity or convergence is justified.
7) Check that every symbol is defined before it is used and that the notation stays consistent.
8) Do not check routine arithmetic or calculations. The computer does that better. Focus on the logic and the ideas of the argument.
9) Never invent an error. If you cannot tell whether a step is wrong, report it as UNCLEAR and say what you could not verify.
10) Never mark a step VALID unless you verified it. Quote the document exactly and report only on what is in the document.
