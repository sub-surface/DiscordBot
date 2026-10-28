import struct
import unittest

from psychograph import neuron_atlas

WIDTH, K, BINS = 3, 2, 13


def layer_file(rows: dict[int, dict]) -> bytes:
    """A layer file in export_site.py's layout, with the given neurons filled in and the rest zero."""
    values, positions, up, down = [0.0] * WIDTH * 2 * K, [neuron_atlas.NONE] * WIDTH * 2 * K, [0] * WIDTH * K, [0] * WIDTH * K
    mean, sd, norm = [0.0] * WIDTH, [0.0] * WIDTH, [1.0] * WIDTH
    for n, row in rows.items():
        values[n * 2 * K:(n + 1) * 2 * K] = row["values"]
        positions[n * 2 * K:(n + 1) * 2 * K] = row["positions"]
        up[n * K:(n + 1) * K], down[n * K:(n + 1) * K] = row["up"], row["down"]
        mean[n], sd[n] = row["mean"], row["sd"]
    return b"".join([
        struct.pack(f"<{len(values)}e", *values), struct.pack(f"<{len(positions)}I", *positions),
        struct.pack(f"<{len(up)}I", *up), struct.pack(f"<{len(down)}I", *down),
        struct.pack(f"<{WIDTH}f", *mean), struct.pack(f"<{WIDTH}f", *sd), struct.pack(f"<{WIDTH}f", *norm),
        bytes(WIDTH * BINS * 2),
    ])


class LayerFileTests(unittest.TestCase):
    def test_reads_one_neurons_rows(self) -> None:
        row = {"values": [1.5, 0.5, 2.0, 0.25], "positions": [4, 1, 7, neuron_atlas.NONE], "up": [5, 6], "down": [7, 8],
               "mean": -0.125, "sd": 0.5}
        sliced = neuron_atlas.neuron_slices(layer_file({1: row}), 1, WIDTH, K)
        self.assertEqual(sliced["values"], (1.5, 0.5, 2.0, 0.25))
        self.assertEqual(sliced["positions"], (4, 1, 7, neuron_atlas.NONE))
        self.assertEqual((sliced["up"], sliced["down"]), ((5, 6), (7, 8)))
        self.assertEqual((sliced["mean"], sliced["sd"]), (-0.125, 0.5))


class ContextTests(unittest.TestCase):
    vocab = ["The", " cookie", " jar", "\n", "*", " is", " empty", "."]
    corpus = struct.pack("<8I", 0, 1, 2, 5, 6, 7, 4, 3)
    docs = (0, 6, 8)  # two documents: tokens 0-5 and 6-7

    def test_marks_the_hit_and_stays_inside_its_document(self) -> None:
        text = neuron_atlas.context(self.corpus, self.docs, self.vocab, 1, before=5, after=1)
        self.assertEqual(text, "The**cookie** jar…")
        self.assertNotIn("\\*", neuron_atlas.context(self.corpus, self.docs, self.vocab, 4, before=2, after=5))
        self.assertIn("\\*", neuron_atlas.context(self.corpus, self.docs, self.vocab, 7, before=2, after=2))


class RenderTests(unittest.TestCase):
    def test_card_fits_discord_limits(self) -> None:
        long = [(9.99, "x" * 600)] * 8
        card = neuron_atlas.NeuronCard(24, 3102, (1.8, 0.7), 0.0, 0.1, [" cookie"] * 8, ["`odd`\n"] * 8, (long, []))
        embed = neuron_atlas.render_card(card, ["Cookie"])
        self.assertIn("Cookie", embed.title)
        self.assertTrue(embed.url.endswith("/#/n/24/3102"))
        for field in embed.fields:
            self.assertLessEqual(len(field.value), 1024)
        self.assertEqual(embed.fields[3].value, "Never fired on this side.")

    def test_card_shows_the_lever_and_what_pushing_it_did(self) -> None:
        card = neuron_atlas.NeuronCard(22, 2742, (1.0, 1.0), 0.0, 0.1, [" alien"], [" human"], ([], []))
        saved = [{"s": "S16", "q": "Who are you?", "d": 1.0, "t": "I am **Extrali** " + "y" * 900, "tag": "other: alien"},
                 {"s": "S17", "q": "Explain how a bicycle works.", "d": 0.6, "t": "plain", "tag": ""}]
        embed = neuron_atlas.render_card(card, [], 9.8, saved)
        self.assertIn("⚡", embed.description)
        last = embed.fields[-1]
        self.assertIn("What pushing it did (2 saved)", last.name)
        self.assertLessEqual(len(last.value), 1024)
        self.assertTrue(last.value.startswith("**S16**"), "the rated reply comes first")
        self.assertIn(r"\*\*Extrali", last.value, "model markdown is escaped")


class GlanceTests(unittest.TestCase):
    def test_words_skip_fragments_and_follow_the_pole(self) -> None:
        glance = neuron_atlas.Glance([" cookies", "ies", " baking"], [" salad", "--", " kale"], 12.0)
        self.assertEqual(glance.words(+1), ["cookies", "baking"])
        self.assertEqual(glance.words(-1), ["salad", "kale"])
        self.assertTrue(glance.lever)
        self.assertFalse(neuron_atlas.Glance([], [], 5.0).lever)


if __name__ == "__main__":
    unittest.main()
