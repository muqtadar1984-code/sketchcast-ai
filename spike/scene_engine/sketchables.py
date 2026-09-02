"""Draw the things the narration NAMES.

A lesson that says "a towering tree", "a tiny ant", "the biggest whale" while
the board stays empty wastes the medium — a whiteboard teacher sketches the
thing as they say it. The visual plan only ever covers the chapter's ROOT
diagram, so this module fills the gap deterministically: it scans a segment's
narration for concrete, sketchable nouns and hands back asset requests the
compiler turns into hand-drawn sketches cued to the words.

Deliberately a CURATED lexicon, not a part-of-speech guess: a wrong noun
costs an image generation and a confusing doodle, so only words that reliably
draw well are listed. Each entry is cached under its own asset key, so the
hundredth lesson that mentions a tree pays nothing for it.
"""

from __future__ import annotations

import re

# noun -> (asset key, drawing prompt). Keys are shared across lessons and
# subjects on purpose: one cached sketch per concept, forever.
_LEXICON: dict[str, tuple[str, str]] = {}


def _add(words: str, key: str, prompt: str) -> None:
    for w in words.split(","):
        _LEXICON[w.strip()] = (key, prompt)


# living things
_add("tree,trees,oak,towering tree", "sk_tree", "A simple leafy tree")
_add("grass,blade of grass", "sk_grass", "A few blades of grass")
_add("flower,flowers", "sk_flower", "A simple flower with petals and a stem")
_add("leaf,leaves", "sk_leaf", "A single leaf with veins")
_add("plant,plants,seedling", "sk_plant", "A small potted plant with leaves")
_add("ant,ants", "sk_ant", "A small ant seen from the side")
_add("whale,whales", "sk_whale", "A large whale seen from the side")
_add("bird,birds", "sk_bird", "A small bird perched, side view")
_add("fish,fishes", "sk_fish", "A simple fish, side view")
_add("dog,dogs,puppy", "sk_dog", "A friendly dog sitting, side view")
_add("cat,cats,kitten", "sk_cat", "A cat sitting, side view")
_add("butterfly,butterflies", "sk_butterfly", "A butterfly with open wings")
_add("bacteria,bacterium,microbe,microbes", "sk_bacteria",
     "Three simple rod-shaped bacteria")
_add("human,person,people,child,student", "sk_person",
     "A simple standing child, front view")
_add("mushroom,fungus,fungi", "sk_mushroom", "A simple mushroom")
_add("seed,seeds", "sk_seed", "A single seed, side view")
_add("root,roots", "sk_roots", "Plant roots spreading below a soil line")
# tools + everyday objects
_add("microscope,microscopes", "sk_microscope", "A laboratory microscope")
_add("magnifying glass,magnifier", "sk_magnifier", "A magnifying glass")
_add("telescope", "sk_telescope", "A telescope on a tripod")
_add("book,books,notebook", "sk_book", "An open book")
_add("brick,bricks", "sk_bricks", "A short stack of bricks in a wall")
_add("castle", "sk_castle", "A simple castle with towers")
_add("house,houses,building,buildings", "sk_house", "A simple house")
_add("car,cars", "sk_car", "A simple car, side view")
_add("ball,balls", "sk_ball", "A round ball")
_add("box,boxes", "sk_box", "A simple cardboard box")
_add("balloon,balloons,water balloon", "sk_balloon", "A balloon on a string")
_add("bottle,bottles", "sk_bottle", "A simple bottle")
_add("cup,cups,glass of water", "sk_cup", "A cup of water")
_add("key,keys", "sk_key", "A simple door key")
_add("clock,clocks", "sk_clock", "A round wall clock")
_add("wall,brick wall", "sk_wall", "A section of brick wall")
_add("door,doors,gate", "sk_door", "A simple door in a frame")
_add("window,windows", "sk_window", "A simple window")
_add("factory,factories,power plant", "sk_factory",
     "A small factory with a chimney")
_add("battery,batteries", "sk_battery", "A single battery cell")
_add("engine,engines,motor", "sk_engine", "A simple engine block")
_add("computer,computers,laptop", "sk_computer", "A laptop computer")
_add("phone,phones,mobile", "sk_phone", "A mobile phone")
# workshop + classroom objects. The founder named "hammer" specifically; these
# are its neighbours — the everyday things a science or maths lesson reaches
# for as an example. Same curated bar as the rest: each must be unambiguous as
# a single line drawing, so no abstractions and no compound scenes.
_add("hammer,hammers", "sk_hammer", "A claw hammer, side view")
_add("nail,nails", "sk_nail", "A single iron nail, side view")
_add("screwdriver,screwdrivers", "sk_screwdriver", "A screwdriver, side view")
_add("spanner,spanners,wrench,wrenches", "sk_spanner", "An open-ended spanner")
_add("saw,handsaw,hand saw", "sk_saw", "A hand saw with a wooden handle")
_add("scissors", "sk_scissors", "A pair of open scissors")
_add("rope,ropes", "sk_rope", "A coiled length of rope")
_add("ladder,ladders", "sk_ladder", "A leaning step ladder")
_add("bucket,buckets", "sk_bucket", "A bucket with a handle")
_add("candle,candles", "sk_candle", "A lit candle")
_add("light bulb,lightbulb,bulb,bulbs", "sk_bulb",
     "A filament light bulb")
_add("pencil,pencils", "sk_pencil", "A sharpened pencil")
_add("chair,chairs", "sk_chair", "A simple wooden chair")
_add("table,tables,desk,desks", "sk_table", "A simple wooden table")
_add("bicycle,bicycles,bike,bikes", "sk_bicycle", "A bicycle, side view")
_add("boat,boats,ship,ships", "sk_boat", "A small boat with a sail")
_add("train,trains", "sk_train", "A simple train engine, side view")
_add("aeroplane,airplane,plane,planes", "sk_plane",
     "A simple aeroplane, side view")
_add("umbrella,umbrellas", "sk_umbrella", "An open umbrella")
_add("bell,bells", "sk_bell", "A hand bell")
_add("drum,drums", "sk_drum", "A simple drum")
_add("guitar,guitars", "sk_guitar", "An acoustic guitar")
_add("kite,kites", "sk_kite", "A diamond kite with a tail")
_add("brush,paintbrush", "sk_brush", "A paintbrush")
_add("funnel,funnels", "sk_funnel", "A laboratory funnel")
_add("syringe,syringes", "sk_syringe", "A medical syringe")
_add("mirror,mirrors", "sk_mirror", "A hand mirror")
_add("prism,prisms", "sk_prism", "A triangular glass prism")
_add("lens,lenses", "sk_lens", "A convex lens seen edge-on")
_add("pendulum,pendulums", "sk_pendulum", "A pendulum on a string")
_add("gear,gears,cog,cogs", "sk_gear", "Two interlocking gears")
_add("screw,screws,bolt,bolts", "sk_screw", "A single screw, side view")
_add("chain,chains", "sk_chain", "A few links of chain")
_add("hook,hooks", "sk_hook", "A metal hook")
_add("switch,switches", "sk_switch", "A simple electrical switch")
_add("wire,wires,cable,cables", "sk_wire", "A length of electrical wire")
_add("pipe,pipes", "sk_pipe", "A section of pipe")
_add("tent,tents", "sk_tent", "A simple ridge tent")
_add("bridge,bridges", "sk_bridge", "A simple arch bridge")
_add("basket,baskets", "sk_basket", "A woven basket")
_add("coin,coins", "sk_coin", "A round coin")
_add("dice,die", "sk_dice", "A single six-sided die")
_add("cube,cubes", "sk_cube", "A simple cube in perspective")
_add("sphere,spheres", "sk_sphere", "A shaded sphere")
_add("cone,cones", "sk_cone", "A simple cone")
_add("cylinder,cylinders", "sk_cylinder", "A simple cylinder")
_add("pyramid,pyramids", "sk_pyramid", "A square-based pyramid")
# nature + physical
_add("sun", "sk_sun", "The sun with rays")
_add("moon", "sk_moon", "A crescent moon")
_add("star,stars", "sk_star", "A five-pointed star")
_add("cloud,clouds", "sk_cloud", "A fluffy cloud")
_add("rain,raindrop,raindrops", "sk_rain", "A cloud with falling raindrops")
_add("water,water drop,droplet", "sk_water_drop", "A single water droplet")
_add("mountain,mountains", "sk_mountain", "A mountain with a peak")
_add("river,rivers", "sk_river", "A winding river between banks")
_add("rock,rocks,stone,stones", "sk_rock", "A few rounded rocks")
_add("soil,earth,ground", "sk_soil", "A cross-section of soil with layers")
_add("fire,flame,flames", "sk_fire", "A simple flame")
_add("ice,ice cube", "sk_ice", "An ice cube")
_add("snowflake,snow", "sk_snowflake", "A snowflake")
_add("wind", "sk_wind", "Curved lines showing wind blowing")
_add("thermometer", "sk_thermometer", "A thermometer")
_add("magnet,magnets", "sk_magnet", "A horseshoe magnet")
_add("spring,springs", "sk_spring", "A coiled spring")
_add("wheel,wheels", "sk_wheel", "A wheel with spokes")
_add("lever,levers", "sk_lever", "A lever balanced on a fulcrum")
_add("pulley,pulleys", "sk_pulley", "A pulley with a rope")
_add("scale,scales,balance", "sk_balance", "A balance scale with two pans")
_add("ruler", "sk_ruler", "A straight ruler")
_add("beaker,beakers,flask", "sk_beaker", "A laboratory beaker with liquid")
_add("test tube,test tubes", "sk_test_tube", "A test tube in a stand")
# food / body (common analogies)
_add("apple,apples", "sk_apple", "An apple with a leaf")
_add("egg,eggs", "sk_egg", "A single egg")
_add("bread,loaf", "sk_bread", "A loaf of bread")
_add("jelly,jam", "sk_jelly", "A wobbly blob of jelly on a plate")
_add("onion,onions", "sk_onion", "An onion bulb")
_add("heart", "sk_heart", "A simple anatomical heart")
_add("brain,brains", "sk_brain", "A simple brain, side view")
_add("bone,bones,skeleton", "sk_bone", "A simple bone")
_add("eye,eyes", "sk_eye", "A simple human eye")
_add("hand,hands", "sk_hand", "An open human hand")

_STOP_BEFORE = re.compile(r"\b(no|not|never|without)\s+$", re.I)
# longest first so "blade of grass" wins over "grass"
_PHRASES = sorted(_LEXICON, key=len, reverse=True)


def find_sketchables(narration: str, limit: int = 2,
                     exclude: set[str] | None = None) -> list[dict]:
    """The concrete things this narration names, in spoken order.

    Returns [{"key", "prompt", "word", "cue"}] — `cue` is the verbatim
    phrase (the noun plus a word of context) the drawing should be timed to.
    At most `limit` per segment: a board that sprouts six doodles teaches
    nothing.
    """
    if not narration:
        return []
    exclude = exclude or set()
    low = narration.lower()
    hits: list[tuple[int, dict]] = []
    used_keys: set[str] = set()
    used_spans: list[tuple[int, int]] = []
    for phrase in _PHRASES:
        key, prompt = _LEXICON[phrase]
        if key in used_keys or key in exclude:
            continue
        for m in re.finditer(r"\b" + re.escape(phrase) + r"\b", low):
            i, j = m.span()
            if _STOP_BEFORE.search(low[max(0, i - 12):i]):
                continue
            if any(i < e and j > s for s, e in used_spans):
                continue
            # cue on the word itself plus the word before it when there is
            # one — enough context to resolve uniquely in the narration
            start = i
            back = low.rfind(" ", 0, i)
            if back > 0:
                back2 = low.rfind(" ", 0, back)
                start = (back2 + 1) if back2 > 0 else i
            hits.append((i, {"key": key, "prompt": prompt, "word": phrase,
                             "cue": narration[start:j].strip()}))
            used_keys.add(key)
            used_spans.append((i, j))
            break
    hits.sort(key=lambda h: h[0])
    return [h[1] for h in hits[:limit]]
