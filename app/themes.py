"""One-click looks: each theme is a partial project patch (colours, fonts, texts, sounds)."""


def _card(title, subtitle, font, color, sub_color, c1, c2, style, sound, bg_type="gradient"):
    return {"title": title, "subtitle": subtitle, "font": font, "color": color, "sub_color": sub_color,
            "bg": {"type": bg_type, "c1": c1, "c2": c2, "style": style, "animated": True},
            "audio": {"type": "sfx", "sfx": sound}}


def _look(font, accent, text, label, badge_text, effect, duration, sound, stinger_text, stinger_sub, s1, s2,
          stinger_sound):
    return {
        "overlays": {"font": font, "accent": accent, "text_color": text,
                     "counter": {"label": label, "badge_text": badge_text},
                     "progress": {"color": accent}},
        "transition": {"effect": effect, "duration": duration, "random": False, "sfx": sound,
                       "stinger": {"text": stinger_text, "subtext": stinger_sub, "c1": s1, "c2": s2,
                                   "text_color": text, "sfx": stinger_sound}},
    }


def _theme(label, look, intro, outro, extra=None):
    patch = dict(look)
    patch["intro"], patch["outro"] = intro, outro
    for k, v in (extra or {}).items():
        patch[k] = v
    return {"label": label, "patch": patch}


THEMES = {
    "classic": _theme(
        "Classic gold",
        _look("Impact", "#ffc400", "#ffffff", "DRINK", "#111111", "fade", 0.6, "airhorn",
              "DRINK!", "{n} down · {left} to go", "#ff2e63", "#7b2ff7", "airhorn3"),
        _card("POWER HOUR", "{count} songs · {count} shots · no mercy", "Impact", "#ffffff", "#ffc400",
              "#2b0548", "#ff2e63", "linear", "gong"),
        _card("YOU SURVIVED", "{count} songs · {minutes} minutes", "Impact", "#ffffff", "#ffc400",
              "#ff2e63", "#2b0548", "radial", "airhorn3")),
    "halloween": _theme(
        "Halloween",
        _look("Chiller", "#ff7518", "#f4f1ea", "SIP", "#1a0b00", "fadeblack", 0.8, "thunder",
              "BOO! DRINK!", "{n} down · {left} to go", "#ff7518", "#22002e", "ghost"),
        _card("HALLOWEEN POWER HOUR", "{count} songs · {count} shots · enter if you dare", "Chiller",
              "#ff7518", "#d9c8ff", "#12001c", "#ff6a00", "radial", "organ"),
        _card("YOU SURVIVED... BARELY", "{count} songs · {minutes} minutes of terror", "Chiller",
              "#ff7518", "#d9c8ff", "#3a0050", "#000000", "spiral", "bell")),
    "christmas": _theme(
        "Christmas",
        _look("Cooper Black", "#e8202a", "#fffaf0", "SIP", "#ffffff", "dissolve", 0.7, "sleigh",
              "HO HO HO!", "{n} down · {left} to go", "#b3000c", "#0b6b2e", "bell"),
        _card("HOLIDAY POWER HOUR", "{count} songs · {count} sips of cheer", "Cooper Black", "#ffffff",
              "#ffd54a", "#0b5d1e", "#b3000c", "radial", "sleigh"),
        _card("MERRY EVERYTHING", "{count} songs · {minutes} minutes", "Cooper Black", "#ffffff",
              "#ffd54a", "#b3000c", "#0b5d1e", "linear", "bell")),
    "stpatricks": _theme(
        "St. Patrick's Day",
        _look("Showcard Gothic", "#2ecc40", "#ffffff", "PINT", "#06240f", "wiperight", 0.6, "clink",
              "SLAINTE!", "{n} down · {left} to go", "#0b8a3c", "#ffb300", "clink"),
        _card("SHAMROCK POWER HOUR", "{count} songs · {count} shots · luck not included", "Showcard Gothic",
              "#ffffff", "#ffd54a", "#044d22", "#2ecc40", "linear", "clink"),
        _card("TOP O' THE HOUR", "{count} songs · {minutes} minutes", "Showcard Gothic", "#ffffff",
              "#ffd54a", "#2ecc40", "#044d22", "radial", "airhorn3")),
    "birthday": _theme(
        "Birthday bash",
        _look("Cooper Black", "#ff4fa3", "#ffffff", "SHOT", "#2b0020", "zoomin", 0.6, "party_horn",
              "CHEERS!", "{n} down · {left} to go", "#ff4fa3", "#3ec6ff", "party_horn"),
        _card("BIRTHDAY POWER HOUR", "{count} songs · {count} shots · make a wish", "Cooper Black",
              "#ffffff", "#fff35c", "#ff4fa3", "#6c3cff", "circular", "party_horn"),
        _card("ANOTHER YEAR SURVIVED", "{count} songs · {minutes} minutes", "Cooper Black", "#ffffff",
              "#fff35c", "#6c3cff", "#ff4fa3", "radial", "party_horn")),
    "newyear": _theme(
        "New Year's Eve",
        _look("Broadway", "#f5c542", "#ffffff", "DRINK", "#141008", "fadewhite", 0.5, "party_horn",
              "CHEERS!", "{n} down · {left} to go", "#141008", "#f5c542", "clink"),
        _card("NEW YEAR'S POWER HOUR", "{count} songs · {count} shots · see you next year", "Broadway",
              "#f5c542", "#ffffff", "#000000", "#3d2c00", "radial", "gong"),
        _card("HAPPY NEW YEAR", "{count} songs · {minutes} minutes", "Broadway", "#f5c542", "#ffffff",
              "#3d2c00", "#000000", "spiral", "airhorn3")),
    "neon80s": _theme(
        "Neon 80s",
        _look("Bahnschrift", "#ff2bd6", "#ffffff", "RAD", "#12002b", "pixelize", 0.6, "scratch",
              "TOTALLY DRINK!", "{n} down · {left} to go", "#ff2bd6", "#00e5ff", "slide_whistle"),
        _card("TOTALLY RADICAL POWER HOUR", "{count} songs · {count} shots · no rewind", "Bahnschrift",
              "#00e5ff", "#ff2bd6", "#12002b", "#ff2bd6", "square", "scratch"),
        _card("GAME OVER", "{count} songs · {minutes} minutes · insert coin", "Bahnschrift", "#00e5ff",
              "#ff2bd6", "#ff2bd6", "#12002b", "spiral", "slide_whistle")),
    "centurion": _theme(
        "Centurion (100 songs)",
        _look("Stencil", "#c9302c", "#ffffff", "SHOT", "#ffffff", "cut", 0.0, "buzzer",
              "DRINK!", "{n} down · {left} to go", "#c9302c", "#1b1b1b", "buzzer"),
        _card("CENTURION", "{count} songs · {count} shots · glory or death", "Stencil", "#ffffff",
              "#c9302c", "#1b1b1b", "#5a0d0b", "linear", "gong"),
        _card("AVE, CENTURION", "{count} songs · {minutes} minutes", "Stencil", "#ffffff", "#c9302c",
              "#5a0d0b", "#1b1b1b", "radial", "airhorn3"),
        extra={"settings": {"target_minutes": 100, "clip_seconds": 60}}),
}


def catalog():
    return [{"id": k, "label": v["label"]} for k, v in THEMES.items()]
