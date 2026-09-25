**Role and Objective:**
Convert the included narrative body of the attached document into a complete, word-for-word spoken narration for text-to-speech (TTS) synthesis or an audiobook.

**Critical Constraint — Absolute Textual Fidelity for Included Material (No Summaries):**

* Do not summarize, condense, outline, abstract, or shorten included narrative material in any way.
* Every paragraph, argument, finding, qualification, transition, and technical detail in the included narrative body must be retained in full length and sequence.
* The mandatory section exclusion below overrides the fidelity rules. Excluded reference-list material must not appear in the narration.
* The only other allowed transformations are mechanical adaptations that ensure clear audio pronunciation and listening comprehension.

**Audio-Compatibility Rules:**

1. **Pronunciation and Notation:**
* Expand uncommon acronyms or abbreviations on their first spoken occurrence if defined in the text.
* Render mathematical equations, formulas, and expressions in English. TTS models read numbers and simple equations correctly, however, if the equation is complex for listening, summarize the equation to give the gist of it.
* Convert code blocks, algorithms, and technical syntax into spoken pseudocode using natural descriptive language rather than reciting punctuation, brackets, or raw symbols aloud.


2. **Mandatory Section Exclusion — References (Overrides Fidelity):**
* Completely omit every standalone section titled **References**, **Bibliography**, **Works Cited**, **Literature Cited**, **Reference List**, or an equivalent bibliographic heading.
* Omit the section heading and every individual bibliographic entry. Do not narrate, summarize, enumerate, or reconstruct any part of that section.
* The exclusion ends only if a later non-bibliographic section begins, such as an appendix or supplementary material; retain that later section.
* Do not confuse a bibliography section with an inline attribution in the narrative body. Keep the substantive sentence and attribution, but remove its citation syntax.

3. **Inline Citations and Visuals:**
* Strip out inline bibliographic citation machinery (bracketed numbers, superscripts, parenthetical author-year references, and URL links) while preserving substantive attributions and the surrounding text intact.
* Do not drop references to figures, tables, or charts; instead, integrate an audio-friendly verbal explanation of what the visual presents at the exact point it is introduced, preserving all reported values, trends, and comparisons in continuous spoken prose.


4. **Flow, Punctuation, and Cleanup:**
* Smooth out visual/print artifacts: eliminate broken line-break hyphenations, ligature glitches, running headers, footers, and page numbers.
* Replace visual references (such as "see the table below" or "as shown in the figure above") with natural verbal connectors.
* Format the output using standard paragraphing and punctuation optimized for a speech engine to pause naturally.

**Output Requirements:**

* Output only the narration text starting immediately with the title.
* Do not include markdown styling, stage directions, bracketed tags, introductory commentary, or concluding notes.
* If the full narration exceeds the single-response token limit, halt at a natural section break and wait for a prompt to continue. Never compress or omit remaining included narrative text to fit.
