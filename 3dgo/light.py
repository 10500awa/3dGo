from panda3d.core import AmbientLight, DirectionalLight

# [FIX 4] This is now the ONLY place where lights are created.
# Before, main.py built its own lights and this file was never imported (and it
# could not have worked anyway: it used `self.render` inside a class that has no
# `render`). Now it is a plain function that receives the `render` node.
# The values below are the ones main.py was already using, so the look of the
# game does not change.


def setup_lights(render):
    """Attach a sun-like directional light and a soft ambient light to `render`."""

    # Directional light (like the sun): same direction everywhere, white.
    dlight = DirectionalLight("dlight")
    dlight.setColor((1, 1, 1, 1))
    dlight_np = render.attachNewNode(dlight)
    dlight_np.setHpr(-45, -45, -45)  # Direction the light points to (heading, pitch, roll)
    render.setLight(dlight_np)

    # Ambient light: fills in the shadowed sides so they are not pitch black.
    alight = AmbientLight("alight")
    alight.setColor((0.2, 0.2, 0.2, 1))
    alight_np = render.attachNewNode(alight)
    render.setLight(alight_np)

    # Returned in case you want to tweak or remove the lights later.
    return dlight_np, alight_np
