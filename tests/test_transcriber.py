from mic2md.transcriber import clean_text


def test_clean_text():
    assert clean_text(" [BLANK_AUDIO] ") == ""
    assert clean_text("(music) Hello there.") == "Hello there."
    assert clean_text("Thank you.") == ""
    assert clean_text("Tack för att ni tittade!") == ""
    assert clean_text("Thank you for the help.") == "Thank you for the help."
