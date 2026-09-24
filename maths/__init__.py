"""Mathematics lessons: the structured worked-example object and everything
derived from it.

The object is the source of truth (founder direction 2026-09-24). A lesson is
generated AS structure — problem, givens, target, typed steps with the state
before and after each, a final answer, a common mistake — and everything
downstream derives from that one record:

    structured example -> SymPy verification (maths.verify)
                       -> board rendering (maths.board -> scene engine)
                       -> narration / captions (maths.speech)
                       -> worksheet and worked answer key (docgen)

Nothing downstream re-reads the mathematics out of prose, so the video, the
voice and the documents cannot disagree about it.

Modules:
    schema    the pydantic record and its JSON schema for the model
    notation  the linear notation both the model and SymPy speak
    verify    step-level and answer verification
    speech    notation -> spoken words, for TTS
    typeset   notation -> a laid-out equation for the board
    board     a verified example -> algebra-board scenes
    lesson    generation, verification, regeneration, and the EpisodeScript
"""
