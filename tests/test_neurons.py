import unittest

from psychograph import neuron_atlas, neurons


class ParsePushesTests(unittest.TestCase):
    def test_reads_the_usual_forms(self) -> None:
        self.assertEqual(neurons.parse_pushes("L24 N3102 +0.8"), [(24, 3102, 0.8)])
        self.assertEqual(neurons.parse_pushes("l7 n4595 -0.8"), [(7, 4595, -0.8)])
        self.assertEqual(neurons.parse_pushes("24:3102:0.8"), [(24, 3102, 0.8)])
        self.assertEqual(neurons.parse_pushes("L17 N1991"), [(17, 1991, neurons.DEFAULT_DOSE)])

    def test_reads_several(self) -> None:
        self.assertEqual(
            neurons.parse_pushes("L14 N8949 +1; L13 N7956 -1"), [(14, 8949, 1.0), (13, 7956, -1.0)]
        )
        self.assertEqual(neurons.parse_pushes("L14 N8949 1, L13 N7956 -1"), [(14, 8949, 1.0), (13, 7956, -1.0)])

    def test_refuses_what_the_model_has_not_got(self) -> None:
        for text in ("L32 N1 +0.5", "L3 N9216", "L3 N5 +2", "cookies", "", "L1 N1; L2 N2; L3 N3; L4 N4; L5 N5"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                neurons.parse_pushes(text)


class ZooTests(unittest.TestCase):
    def test_every_preset_is_a_real_neuron_within_the_dose_limit(self) -> None:
        zoo = neurons.load_zoo()
        self.assertIn("Cookie", zoo)
        self.assertEqual(zoo["Cookie"], [(24, 3102, 0.8)])
        for name, pushes in zoo.items():
            for layer, neuron, dose in pushes:
                with self.subTest(name=name):
                    self.assertTrue(0 <= layer < neurons.LAYERS and 0 <= neuron < neurons.WIDTH)
                    self.assertLessEqual(abs(dose), neurons.MAX_DOSE)

    def test_zoo_lookup_ignores_case(self) -> None:
        self.assertEqual(neurons.zoo_lookup("cookie"), ("Cookie", [(24, 3102, 0.8)]))
        self.assertIsNone(neurons.zoo_lookup("no such neuron"))


class RigTests(unittest.TestCase):
    def test_nudges_round_clamp_and_never_reach_zero(self) -> None:
        self.assertEqual(neurons.scaled([(24, 3102, 0.8)], 0.75), [(24, 3102, 0.6)])
        self.assertEqual(neurons.scaled([(24, 3102, 1.3)], 4 / 3), [(24, 3102, neurons.MAX_DOSE)])
        self.assertEqual(neurons.scaled([(1, 2, -0.05)], 0.5), [(1, 2, -0.05)])
        self.assertEqual(neurons.flipped([(1, 2, 0.8), (3, 4, -1.0)]), [(1, 2, -0.8), (3, 4, 1.0)])

    def test_warnings_follow_the_studies(self) -> None:
        self.assertEqual(neurons.warnings(neurons.Rig("hi", [(24, 3102, 0.8)])), [])
        hot = neurons.warnings(neurons.Rig("hi", [(1, 1, 1.2), (1, 2, -1.0)]))
        self.assertTrue(any("hot stack" in w for w in hot) and any("share a layer" in w for w in hot))

    def test_sharp_lever_warns_only_when_pushed_hard_on_its_pole(self) -> None:
        sharp = {(24, 3102): neuron_atlas.Glance([], [], 12.0)}
        self.assertIn("S20", neurons.warnings(neurons.Rig("hi", [(24, 3102, 0.8)]), sharp)[0])
        self.assertEqual(neurons.warnings(neurons.Rig("hi", [(24, 3102, 0.4)]), sharp), [])
        self.assertEqual(neurons.warnings(neurons.Rig("hi", [(24, 3102, -0.8)]), sharp), [])

    def test_dial_shows_dose_lever_and_words(self) -> None:
        line = neurons.dial((24, 3102, 0.8), ["cookies"], True)
        self.assertIn("▰▰▰▱▱▱", line)
        self.assertIn("⚡", line)
        self.assertIn("*cookies*", line)
        self.assertNotIn("⚡", neurons.dial((24, 3102, -0.8), [], True), "lens z is about the + pole")

    def test_render_fits_and_hides_the_dip(self) -> None:
        from psychograph.neuron_atlas import Glance

        rig = neurons.Rig("hi", [(24, 3102, 0.8), (13, 7956, -1.3)], name="Cookie", reply="x" * 5000, plain="plain", takes=2)
        embed = neurons.render(rig, {(24, 3102): Glance([" cookies"], [" kale"], 12.0)})
        self.assertEqual(embed.title, "🧠 Cookie")
        self.assertLessEqual(len(embed.description), 3000)
        self.assertLessEqual(len(embed), 6000)
        self.assertIn("bench?p=24:3102:0.8,13:7956:-1.3", embed.url)
        self.assertIn("take 2", embed.footer.text)
        self.assertEqual([f.name for f in embed.fields], ["Dials", "Unsteered", "Heads up"])
        dip = neurons.Rig("hi", [(22, 2742, 0.6)], name="Lucky dip", secret="alien", reply="I am Extrali")
        embed = neurons.render(dip, {(22, 2742): Glance([" alien"], [], 9.8)})
        self.assertTrue(embed.title.startswith("🎲"))
        self.assertIsNone(embed.url, "the atlas link would give the dip away")
        self.assertNotIn("alien", embed.fields[0].value)
        self.assertIn("||alien||", embed.fields[1].value)

    def test_a_fork_is_its_own_rig(self) -> None:
        rig = neurons.Rig("hi", [(1, 2, 0.8)], owner=1, takes=3, busy=True)
        fork = rig.fork(2)
        fork.pushes.append((3, 4, 0.5))
        self.assertEqual((fork.owner, fork.takes, fork.busy), (2, 0, False))
        self.assertEqual(rig.pushes, [(1, 2, 0.8)])


class VoiceTests(unittest.TestCase):
    def test_a_voice_becomes_floors_over_only_its_directions(self) -> None:
        import base64
        import json
        import struct
        import tempfile
        from pathlib import Path

        directions = [base64.b64encode(struct.pack("<4e", *[float(i)] * 4)).decode() for i in range(3)]
        data = {"recipe": "W+S", "directions": directions, "voices": {
            "Martin Heidegger": {"short": "Heidegger", "window": [8, 9], "floors": [[8, 2, 1.5], [8, 1, 0.5], [9, 2, 2.0]]}}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "voices.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            voice = neurons.load_voices(path)["Martin Heidegger"]
            pushes, dirs = neurons.voice_request(voice, path)
        self.assertEqual(voice.recipe, "W+S")
        self.assertEqual(pushes, [[8, -1, 1.5, "floor"], [8, -2, 0.5, "floor"], [9, -1, 2.0, "floor"]])
        self.assertEqual(dirs, [[2.0] * 4, [1.0] * 4])
        rig = neurons.Rig("hi", voice=voice)
        self.assertEqual(rig.title, "🎙️ as Heidegger")
        self.assertIn("shared voice", neurons.render(rig).fields[0].value)


class LinkTests(unittest.TestCase):
    def test_the_bundled_copy_matches_the_hub_zoo(self) -> None:
        # The bot reads ml/mechinterp/zoo.json live; data/neurons.json is its fallback outside the hub.
        if not neurons.HUB_ZOO.is_file():
            self.skipTest("not inside the Psychograph hub")
        self.assertEqual(neurons.load_zoo(neurons.HUB_ZOO), neurons.load_zoo(neurons.BUNDLED_ZOO),
                         "data/neurons.json is out of date: copy ml/mechinterp/zoo.json over it (dashboard: n → s)")

    def test_help_card_fits(self) -> None:
        from psychograph.cogs.help import neurons_embed

        embed = neurons_embed()
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))
        self.assertLessEqual(len(embed), 6000)
        self.assertIn("Cookie", embed.fields[-1].value)
