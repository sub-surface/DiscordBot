import asyncio
import random
import unittest
from types import SimpleNamespace

import discord

from psychograph import demon_art
from psychograph import outside as xeno
from psychograph.cogs.outside import OutsideCog, demon_card, quote_view

from .helpers import make_bot


class ArithmeticTests(unittest.TestCase):
    def test_aq_counts_digits_at_face_value_and_letters_from_ten(self) -> None:
        self.assertEqual(xeno.aq("A"), 10)
        self.assertEqual(xeno.aq("Z"), 35)
        self.assertEqual(xeno.aq("9z"), 44)
        self.assertEqual(xeno.aq("Nick Land"), xeno.aq("NICK-LAND!"))  # case, spaces and punctuation count nothing
        self.assertEqual(xeno.reduce(333), 9)

    def test_tic_xenotation_matches_barkers_matrix_in_tic_talk(self) -> None:
        matrix = ["((-P)):", "(-P):", ":", "(:)", "::", "((:))", ":(:)", "(::)", ":::", "(:)(:)"]  # Fanged Noumena p. 609
        self.assertEqual([xeno.tx(n) for n in range(10)], matrix)
        self.assertEqual(xeno.tx(55), "((:))(((:)))")      # 5 × 11, p. 609
        self.assertEqual(xeno.tx(127), "(((((:)))))")      # the 31st prime, p. 608
        self.assertEqual(xeno.tx(149), "(((:))(::))")      # the 35th prime, p. 610
        with self.assertRaises(ValueError):
            xeno.tx(xeno.TX_LIMIT)

    def test_gates_are_cumulations_reduced(self) -> None:
        self.assertEqual([xeno.gate(zone) for zone in range(10)], [0, 1, 3, 6, 1, 6, 3, 1, 9, 9])


class PandemoniumTests(unittest.TestCase):
    def test_forty_five_demons_one_per_pair_of_zones(self) -> None:
        demons = xeno.demons()
        self.assertEqual(len(demons), 45)
        self.assertEqual({tuple(sorted(d.zones)) for d in demons}, {(low, high) for high in range(10) for low in range(high)})
        self.assertEqual({d.name for d in demons if d.syzygy}, {"Katak", "Djynxx", "Oddubb", "Mur Mur", "Uttunul"})

    def test_a_summoning_by_name_is_always_the_same_demon(self) -> None:
        self.assertEqual(xeno.summon("Hugh Chungus"), xeno.summon("hugh chungus!"))
        self.assertIn(xeno.summon(rng=random.Random(1)), xeno.demons())

    def test_every_demon_has_its_own_likeness_that_fits_a_phone(self) -> None:
        arts = [demon_art.avatar(demon) for demon in xeno.demons()]
        self.assertEqual(len(set(arts)), 45)
        for demon, art in zip(xeno.demons(), arts):
            lines = art.split("\n")
            self.assertLessEqual(max(map(len, lines)), 22, demon.name)
            self.assertLessEqual(len(lines), 16, demon.name)
            self.assertIn(demon.span, art)
            self.assertTrue(art.isascii(), demon.name)
            self.assertNotIn("```", art)
        self.assertEqual(demon_art.avatar(xeno.demons()[0]), demon_art.avatar(xeno.demons()[0]))

    def test_the_card_carries_the_numogram_and_the_likeness(self) -> None:
        async def build():
            return demon_card(xeno.demons()[14], "Hugh", 236)

        view, file = asyncio.run(build())
        self.assertEqual(file.filename, "numogram.png")
        [container] = view.children
        texts = "\n".join(getattr(child, "content", "") for child in container.walk_children() if isinstance(child, discord.ui.TextDisplay))
        self.assertIn("5::4 · Katak", texts)
        self.assertIn("a syzygy", texts)
        self.assertIn("AQ 236", texts)


class RequestTests(unittest.TestCase):
    def test_what_counts_as_asking(self) -> None:
        self.assertEqual(xeno.request("aq: Nick Land"), ("aq", "Nick Land"))
        self.assertEqual(xeno.request("tx 2,026"), ("tx", "2026"))
        self.assertEqual(xeno.request("summon"), ("summon", ""))
        self.assertEqual(xeno.request("summon: will it work"), ("summon", "will it work"))
        self.assertEqual(xeno.request("draw a demon for Hugh"), ("summon", "Hugh"))
        self.assertEqual(xeno.request("fragment"), ("fragment", ""))
        self.assertEqual(xeno.request("Fragment: the death of god"), ("fragment", "the death of god"))
        for chat in ("summon the council", "summoning", "aqua is a colour", "tx is short for transaction", "taxes",
                     "fragmented thoughts tonight", "a fragment of the old wall fell"):
            self.assertIsNone(xeno.request(chat), chat)


class BankTests(unittest.TestCase):
    def setUp(self) -> None:
        rows = [
            ("fn-1", "The body without organs is the cosmic egg: virtual matter that reprograms time."),
            ("fn-2", "Capitalism is still accelerating, even though it has already realized novelties beyond imagining."),
            ("fn-3", "Machinic desire can seem a little inhuman, as it rips up political cultures and deletes traditions."),
        ] + [(f"fn-{n}", f"Filler sentence number {n} about weather, gardens and the price of bread.") for n in range(4, 400)]
        # (as big as the real bank: rare words weigh what they will in use)
        self.bank = xeno.Bank([xeno.Quote(id, text, text, "Nick Land", "Fanged Noumena", 2011, 1) for id, text in rows])

    def test_a_shared_phrase_finds_the_line(self) -> None:
        found = self.bank.match("can someone explain the body without organs to me")
        self.assertIsNotNone(found)
        self.assertEqual(found[0].id, "fn-1")

    def test_common_words_alone_find_nothing(self) -> None:
        self.assertIsNone(self.bank.match("it's still pretty good even though I already said something else"))
        self.assertIsNone(self.bank.match("ok"))

    def test_asking_about_a_subject_or_an_author(self) -> None:
        hegel = xeno.Quote("ps-1", "The true is the whole.", "", "G. W. F. Hegel", "Phenomenology of Spirit", 1807, 11)
        bank = xeno.Bank(self.bank.quotes + [hegel])
        for _ in range(10):
            self.assertIn(bank.fragment("tell me about desire").id, {"fn-3"})
            self.assertEqual(bank.fragment(author="G. W. F. Hegel").id, "ps-1")
        self.assertIsNotNone(bank.fragment("zzzz nothing shares this"))  # falls back to chance
        self.assertEqual(bank.author_named("hegel"), "G. W. F. Hegel")
        self.assertEqual(bank.author_named("Nick Land"), "Nick Land")
        self.assertIsNone(bank.author_named("Kant"))
        self.assertEqual(bank.authors[0], "Nick Land")  # the most quoted first

    def test_a_week_has_one_theme_from_the_big_ones(self) -> None:
        from datetime import date

        quotes = [xeno.Quote(f"x-{n}", f"line {n}", "", "Plato", "Dialogues", -380, 1, theme)
                  for n, theme in enumerate(["Death"] * 20 + ["the death"] * 15 + ["time"] * 40 + ["god"] * 5)]
        bank = xeno.Bank(quotes)
        self.assertEqual(set(bank.themes()), {"death", "time"})  # "Death" and "the death" fold together; god is too small
        monday, sunday, next_week = date(2026, 10, 26), date(2026, 11, 1), date(2026, 11, 2)
        self.assertEqual(bank.week_theme(monday), bank.week_theme(sunday))
        self.assertIn(bank.week_theme(next_week), {"death", "time"})
        self.assertIsNone(xeno.Bank([]).week_theme(monday))

    def test_an_unbuilt_bank_is_empty_not_broken(self) -> None:
        from pathlib import Path

        empty = xeno.Bank.load(Path("does-not-exist.json"))
        self.assertEqual(empty.quotes, [])
        self.assertIsNone(empty.match("the body without organs is the cosmic egg"))
        self.assertIsNone(empty.random())
        self.assertIsNone(empty.fragment("anything"))

    def test_a_quote_card_cannot_ping(self) -> None:
        quote = xeno.Quote("fn-9", "@everyone the outside is coming", "", "Nick Land", "Fanged Noumena", 2011, 3)

        async def build():
            return quote_view(quote)

        [container] = asyncio.run(build()).children
        text = next(child.content for child in container.children if isinstance(child, discord.ui.TextDisplay))
        self.assertNotIn("@everyone", text.replace("@​everyone", ""))


class SecretTests(unittest.TestCase):
    def test_summoning_the_bot_summons_nothing(self) -> None:
        bot = make_bot()
        cog = OutsideCog(bot)
        bot._connection.user = SimpleNamespace(display_name="Mechirk Spookstein", id=1)

        async def build(name):
            return cog.summoning(name, SimpleNamespace())

        for name in ("Mechirk Spookstein", "mecha epstein"):  # its name now, and what it was before
            view, _file = asyncio.run(build(name))
            [container] = view.children
            self.assertIn("no forty-sixth demon", container.children[0].content)
        bot.store.close()
