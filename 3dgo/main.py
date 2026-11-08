from panda3d.core import NodePath, CollisionNode, CollisionSphere, CollisionTraverser, CollisionHandlerQueue, CollisionRay, TextNode
from panda3d.core import AntialiasAttrib, ClockObject, Material, Point3, LineSegs
from panda3d.core import loadPrcFile
from direct.showbase.ShowBase import ShowBase
from direct.gui.OnscreenText import OnscreenText
from direct.gui.DirectGui import DirectEntry, DGG, DirectOptionMenu, DirectButton
import hashlib
import json
import os

from camera import Camera
from light import setup_lights

globalClock = ClockObject.getGlobalClock()

# Load configuration file
loadPrcFile("config/conf.prc")

# Size of a stone model. It was the magic number 0.49 repeated everywhere; now it's one constant.
STONE_SCALE = 0.49

# ---------------------------------------------------------------------------------------
# CHANGE LOG (search for the "[FIX n]" tags in the code to find every change)
#  [FIX 1] Performance: all grid points/lines hang under ONE node (grid_root) and all stones
#          under ONE node (stone_root). Clearing the board is now one removeNode() call
#          instead of looping over thousands of nodes. The grid lines are also built once
#          instead of twice, and the little blue (0,0,0) ball is no longer leaked.
#  [FIX 2] Byo-yomi reset was dead code (an `elif` that could never run). Fixed.
#          pass_turn also started the timer even with "No Timer" (it would end the game by
#          "timeout" after a while). Fixed.
#  [FIX 3] Real positional SUPERKO: a move is illegal if it recreates ANY earlier position,
#          not only the one from two moves ago (that was only simple ko).
#  [FIX 4] Lighting only in light.py.
#  [FIX 5] create_stone() / clear_stones() / restore_state() helpers: stone creation code
#          was copy-pasted in spawn_model, load_game and rewind_turn.
#  [FIX 6] Passes are now recorded in match_history (so rewind and save/load see them),
#          and rewinding resets pass_count / game_ended / the "Game Over" text.
# ---------------------------------------------------------------------------------------


class GridDemo(ShowBase):
    def __init__(self, size):
        super().__init__()

        # ========================================== #
        # 1. ENGINE & GRAPHICS SETUP                 #
        # ========================================== #
        self.custom_font = self.loader.loadFont("fonts/Montserrat-Regular.ttf") # I like this font, didn't like the default one
        self.background_color = (0.5, 0.5, 0.5)                # Background gray, so your eyes don't get annihilated.
        self.setBackgroundColor(self.background_color)

        self.myMaterial = Material()                           # Material/shine of the stones
        self.myMaterial.setShininess(16)
        self.myMaterial.setSpecular((0.25, 0.25, 0.25, 1))

        setup_lights(self.render)   # [FIX 4] was ~10 lines duplicated here, now lives in light.py

        # Models & textures. For very big boards you could use a cube model instead of the sphere (faster).
        self.ball_model = self.loader.loadModel("models/sphere.bam")
        self.black_texture = self.loader.loadTexture("textures/black.png")
        self.white_texture = self.loader.loadTexture("textures/white.png")

        # ========================================== #
        # 2. GAME STATE & GRID VARIABLES             #
        # ========================================== #
        self.initial_size = size
        self.size = "0,0,0"
        self.x_size = 1
        self.y_size = 1
        self.z_size = 1

        self.black_points = 0
        self.white_points = 0
        self.black_captures = 0
        self.white_captures = 0
        self.komi = 0.5

        self.current_color = 0  # 0 = black, 1 = white
        self.turn = 1
        self.pass_count = 0     # 2 -> game ends as both players pass one after the other
        self.game_ended = False # becomes True when pass_count reaches 2 (or on timeout)
        self.match_history = [] # One snapshot per turn (moves AND passes). Used for superko, rewind and save/load.

        # [FIX 1] Parent nodes. Everything we create is attached below one of these two,
        # so removing a whole board is a single call.
        self.grid_root = self.render.attachNewNode("grid_root")    # grid points, grid lines, blue (0,0,0) ball
        self.stone_root = self.render.attachNewNode("stone_root")  # every stone

        self.nodes = []         # one NodePath per grid point (each one holds its collision sphere)
        self.line_nodes = []    # one line-set per z level
        self.balls = {}         # balls[(x,y,z)] -> {'node': NodePath, 'color': 0/1}

        self.last_plane_action = None
        self.current_plane = -1
        self.layer_count = 0
        self.grid_color = (1, 1, 1)

        # ========================================== #
        # 3. TIMER VARIABLES                         #
        # ========================================== #
        self.timer_running = False    # By default no timer is on.
        self.timer_task = None
        self.timer_mode = "No Timer"

        self.main_time = 0            # All of this changes by choosing a timer mode and configuring it.
        self.increment = 0
        self.byo_periods = 5
        self.byo_time = 30

        self.black_time = self.main_time
        self.white_time = self.main_time
        self.black_byo_periods_left = self.byo_periods
        self.white_byo_periods_left = self.byo_periods
        self.black_byo_yomi_time = self.byo_time
        self.white_byo_yomi_time = self.byo_time
        self.black_byo_yomi_periods = self.byo_periods
        self.white_byo_yomi_periods = self.byo_periods

        # ========================================== #
        # 4. INITIALIZATION ROUTINES                 #
        # ========================================== #
        self.camera_control = Camera(self, self.initial_size) # camera.py: orbit camera around the board center
        self.setup_collision_system()  # Mouse ray + collision traverser used to click on grid points
        self.disableMouse()            # Turns off Panda3D's default mouse camera so camera.py is the only one moving it
        self.bind_inputs()             # Connects keys/mouse buttons to functions (see bind_inputs)

        self.extra_gui_elements = {}
        self.help_show = 0
        self.extra_show = 0

        self.setup_gui()
        self.game_over_text = OnscreenText(
            text="To start, select a grid size and press enter. e.g (4), (19,19), (10,10,2)",
            pos=(0, 0), scale=0.1, fg=(0, 0, 0, 1), align=TextNode.ACenter, font=self.custom_font
        )

    # ========================================== #
    # SAVE, LOAD & RESET                         #
    # ========================================== #
    def save_game(self):
        if self.turn == 1:
            return
        if not os.path.exists("./save"):
            os.makedirs("./save")

        # JSON can't use tuples as dict keys, so (x,y,z) is turned into the string "x,y,z".
        serializable_history = []
        for state in self.match_history:
            serializable_balls = {f"{k[0]},{k[1]},{k[2]}": v for k, v in state['balls'].items()}
            serializable_history.append({
                'balls': serializable_balls,
                'current_color': state['current_color'],
                'turn': state['turn'],
                'black_captures': state['black_captures'],
                'white_captures': state['white_captures'],
                'pass_count': state.get('pass_count', 0),   # [FIX 6] passes are saved too
                'board_hash': state['board_hash']
            })

        payload = {
            'size_str': self.size,
            'match_history': serializable_history
        }
        with open("./save/savegame.json", "w") as f:
            json.dump(payload, f, indent=4)

    def load_game(self):
        path = "./save/savegame.json"
        if not os.path.exists(path):
            print("No save file found.")
            return
        try:
            with open(path, "r") as f:
                payload = json.load(f)

            # Rebuild an empty board of the saved size first.
            self.size = payload['size_str']
            self.reset_game()
            self.generate_grid(self.size)

            # Turn the "x,y,z" string keys back into (x,y,z) tuples.
            history = []
            for state in payload['match_history']:
                deserialized_balls = {}
                for k_str, v in state['balls'].items():
                    coords = tuple(int(c) for c in k_str.split(','))
                    deserialized_balls[coords] = v
                history.append({
                    'balls': deserialized_balls,
                    'current_color': state['current_color'],
                    'turn': state['turn'],
                    'black_captures': state['black_captures'],
                    'white_captures': state['white_captures'],
                    'pass_count': state.get('pass_count', 0),   # old saves don't have it -> 0
                    'board_hash': state['board_hash']
                })
            self.match_history = history

            # [FIX 5] The last snapshot IS the current position, so we just restore it.
            if self.match_history:
                self.restore_state(self.match_history[-1])
                print("Match loaded successfully.")
        except Exception as e:
            print(f"Error loading game data: {e}")

    def reset_game(self):
        self.game_ended = False
        self.pass_count = 0
        self.white_points = 0
        self.black_points = 0
        self.black_captures = 0
        self.white_captures = 0
        self.current_color = 0
        self.turn = 1
        self.layer_count = 0
        self.match_history = []

        self.clear_stones()   # [FIX 1][FIX 5] one call instead of a loop

        self.pause_timer()
        self.show_everything()

        if hasattr(self, 'turn_text'):
            self.turn_text.setText(f"Turn: {self.turn}")
        if hasattr(self, 'text_captures'):
            self.text_captures.setText(f"Black captures: {self.black_captures}\nWhite captures: {self.white_captures}")
        if hasattr(self, 'text_points'):
            self.text_points.setText(f"Black: {self.black_points} pts\nWhite: {self.white_points} pts")

    # ========================================== #
    # STONE HELPERS  [FIX 5]                     #
    # ========================================== #
    '''
    Creates ONE stone model at `pos` with the color of `color` (0 black, 1 white) and returns
    the dict that goes into self.balls. This used to be copy-pasted in three places.
    '''
    def create_stone(self, pos, color):
        model = self.ball_model.copyTo(self.stone_root)   # [FIX 1] parented to stone_root, not render
        model.setTexture(self.black_texture if color == 0 else self.white_texture, 1)
        model.setScale(STONE_SCALE)
        model.setPos(pos[0], pos[1], pos[2])
        model.setMaterial(self.myMaterial)
        return {'node': model, 'color': color}

    '''
    Removes every stone from the screen and from the balls dict. We delete the parent node
    and make a fresh one, which is much faster than removing the stones one by one.
    '''
    def clear_stones(self):
        self.stone_root.removeNode()
        self.stone_root = self.render.attachNewNode("stone_root")
        self.balls.clear()

    '''
    Removes the "Game Over" / welcome text if it's on screen.
    '''
    def clear_game_over_text(self):
        if getattr(self, 'game_over_text', None) is not None:
            self.game_over_text.destroy()
            self.game_over_text = None

    '''
    Saves the current position as a snapshot in match_history. It's called after every move
    AND after every pass [FIX 6]. 'board_hash' is what superko compares against [FIX 3].
    '''
    def record_state(self):
        self.match_history.append({
            'balls': {k: {'color': v['color']} for k, v in self.balls.items()},
            'current_color': self.current_color,
            'turn': self.turn,
            'black_captures': self.black_captures,
            'white_captures': self.white_captures,
            'pass_count': self.pass_count,
            'board_hash': self.get_board_hash()
        })

    '''
    The position before anybody has played. Used when we rewind all the way to the start.
    '''
    def empty_state(self):
        return {'balls': {}, 'current_color': 0, 'turn': 1,
                'black_captures': 0, 'white_captures': 0, 'pass_count': 0}

    '''
    Puts the whole game back to a snapshot (stones, whose turn, captures, passes, texts).
    Shared by load_game and rewind_turn, which used to have their own copies of this code.
    '''
    def restore_state(self, state):
        self.clear_stones()
        for pos, ball_data in state['balls'].items():
            self.balls[pos] = self.create_stone(pos, ball_data['color'])

        self.current_color = state['current_color']
        self.turn = state['turn']
        self.black_captures = state['black_captures']
        self.white_captures = state['white_captures']
        self.pass_count = state.get('pass_count', 0)   # [FIX 6]
        self.game_ended = False                        # [FIX 6] a restored game is playable again...
        self.clear_game_over_text()                    # [FIX 6] ...so the "Game Over" text must go

        self.turn_text.setText(f"Turn: {self.turn}")
        self.points()                                  # also refreshes the capture / points texts

        if self.pass_count >= 2:                       # [FIX 6] e.g. loading a save of a finished game
            self.end_game()

    # ========================================== #
    # GRID & RENDERING                           #
    # ========================================== #
    def generate_grid(self, size):
        try:
            parts = [int(x.strip()) for x in size.split(',')] # "10,10,2" -> [10, 10, 2]
            self.x_size, self.y_size, self.z_size = parts     # set_grid_size already normalised it to exactly 3 numbers
            # (typing "10" gives 10,10,10 and "19,19" gives 19,19,1, see set_grid_size)

            # [FIX 1] Clear the old board. Everything is under grid_root / stone_root now,
            # so no need to loop over every node, collision node and line.
            self.clear_stones()
            self.grid_root.removeNode()
            self.grid_root = self.render.attachNewNode("grid_root")
            self.nodes = []
            self.line_nodes = []

            # Generate one node per position (intersection of the lines), each with a small
            # collision sphere so we can click on it.
            for x in range(self.x_size):
                for y in range(self.y_size):
                    for z in range(self.z_size):
                        vertex_node = NodePath(f"vertex_{x},{y},{z}")   # the name is parsed in show_one_floor / cut_layer, don't rename
                        vertex_node.setPos(x, y, z)
                        vertex_node.reparentTo(self.grid_root)          # [FIX 1] was self.render
                        collision_sphere = CollisionSphere(0, 0, 0, 0.25)
                        collision_node = vertex_node.attachNewNode(CollisionNode(f'cnode_{x}{y}{z}'))
                        collision_node.node().addSolid(collision_sphere)
                        collision_node.node().setIntoCollideMask(1)
                        collision_node.setColor(0.5, 0.5, 0.5, 1)
                        self.nodes.append(vertex_node)

            # [FIX 1] Grid lines are built once here with the current grid color.
            # Before: built in white here and then deleted and rebuilt by custom_grid_color.
            self.build_grid_lines(*self.grid_color)

            self.clear_game_over_text()    # if the new size is created after a game finished, this removes the text
            self.reset_camera(self.size)   # center the camera on the new board
            self.place_initial_ball()      # the tiny blue ball that marks (0,0,0)
        except Exception as e:
            print(f"Error during grid generation: {e}")
            return

    '''
    [FIX 1] Draws one 2D grid of lines per z level, using color (r,g,b) in 0..1.
    generate_grid and custom_grid_color both used to contain this same loop.
    '''
    def build_grid_lines(self, r, g, b):
        for line_node in self.line_nodes:
            line_node.removeNode()
        self.line_nodes = []

        for z in range(self.z_size):
            lines = LineSegs()
            lines.setThickness(1)
            lines.setColor(r, g, b, 1)
            for y in range(self.y_size):
                lines.moveTo(0, y, z)
                lines.drawTo(self.x_size - 1, y, z)
            for x in range(self.x_size):
                lines.moveTo(x, 0, z)
                lines.drawTo(x, self.y_size - 1, z)
            line_node = self.grid_root.attachNewNode(lines.create())   # under grid_root so it's removed with the board
            line_node.setAntialias(AntialiasAttrib.M_line)             # Antialias, the lines look waaay better with this
            self.line_nodes.append(line_node)

    '''
    This function creates a tiny blue ball using the smiley model (default on panda3d), positioned on (0,0,0).
    [FIX 1] It's now parented to grid_root. Before it was attached to render and never removed,
    so every new grid left one more blue ball behind.
    '''
    def place_initial_ball(self):
        initial_ball = self.loader.loadModel("smiley")
        initial_ball.setPos(0, 0, 0)
        initial_ball.setTexture(self.loader.loadTexture("textures/blue.png"), 1)
        initial_ball.setScale(0.05)
        initial_ball.reparentTo(self.grid_root)

    '''
    This function changes the background color of the game, might affect text readability.
    '''
    def custom_background_color(self, event=None):
        if self.extra_show == 1 and "background_entry" in self.extra_gui_elements:
            text = self.extra_gui_elements["background_entry"].get()
        else:
            if isinstance(event, tuple) and len(event) == 3:
                text = ','.join(str(int(c * 255)) for c in event)
            else:
                text = ','.join(str(int(c * 255)) for c in self.background_color)
        try:
            color = [int(x.strip()) for x in text.split(',')]
            if len(color) != 3: return
            r, g, b = [c / 255.0 for c in color]
            if not all(0 <= c <= 1 for c in (r, g, b)): return
            self.setBackgroundColor(r, g, b)
            self.background_color = (r, g, b)
        except ValueError:
            return

    '''
    This function changes the grid color on the fly. The text comes from the entry box as "r,g,b" (0-255).
    [FIX 1] The line drawing now lives in build_grid_lines.
    '''
    def custom_grid_color(self, event=None):
        if self.extra_show == 1 and "color_entry" in self.extra_gui_elements:
            text = self.extra_gui_elements["color_entry"].get()
        else:
            if isinstance(event, tuple) and len(event) == 3:
                text = ','.join(str(int(c * 255)) for c in event)
            else:
                text = ','.join(str(int(c * 255)) for c in self.grid_color)
        try:
            color = [int(x.strip()) for x in text.split(',')]
            if len(color) != 3: return
            r, g, b = [c / 255.0 for c in color]
            if not all(0 <= c <= 1 for c in (r, g, b)): return

            self.grid_color = (r, g, b)
            self.build_grid_lines(r, g, b)
        except ValueError:
            return

    '''
    Makes nothing visible: scale 0 for every node and ball, hide the lines.
    '''
    def show_nothing(self):
        for node in self.nodes: node.setScale(0.0)
        for ball_data in self.balls.values(): ball_data['node'].setScale(0.0)
        for line_node in self.line_nodes: line_node.hide()

    '''
    This resets the visibility of the balls and grids.
    '''
    def show_everything(self):
        if self.x_size == 1 or self.y_size == 1 or self.z_size == 1: return
        for node in self.nodes: node.setScale(1.0)
        for ball_data in self.balls.values(): ball_data['node'].setScale(STONE_SCALE)
        for line_node in self.line_nodes: line_node.show()

        if self.last_plane_action == 1:
            self.current_plane = (self.current_plane - 1) % self.z_size
        elif self.last_plane_action == 0:
            self.current_plane = (self.current_plane + 1) % self.z_size

    '''
    while on 'show_one_floor' mode, this function moves the plane that's shown 1 floor up.
    '''
    def plane_up(self):
        self.show_one_floor(1, self.current_plane)
        self.last_plane_action = 1

    '''
    while on 'show_one_floor' mode, this function moves the plane that's shown 1 floor down.
    '''
    def plane_down(self):
        self.show_one_floor(0, self.current_plane)
        self.last_plane_action = 0

    '''
    Makes only one floor visible; plane_up / plane_down change which one.
    '''
    def show_one_floor(self, up_or_down, current_z):
        if up_or_down == 0:
            if current_z is not None:
                if not hasattr(self, 'current_plane'): self.current_plane = 0
                else: self.current_plane = (self.current_plane - 1) % self.z_size
                z = self.current_plane
            else:
                z = current_z # If we only have one floor, this function should do nothing.
                if not (0 <= z < self.z_size): return
        else:
            if current_z is not None:
                if not hasattr(self, 'current_plane'): self.current_plane = 0
                else: self.current_plane = (self.current_plane + 1) % self.z_size
                z = self.current_plane
            else:
                z = current_z
                if not (0 <= z < self.z_size): return

        # For every node, ball and line: if it isn't on floor z we scale it to 0 or hide it.
        for node in self.nodes:
            coords = node.getName().split('_')[1].split(',')
            z_pos = float(coords[2])
            node.setScale(1 if int(z_pos) == z else 0)

        for pos, ball_data in list(self.balls.items()):
            z_pos = pos[2]
            ball_data['node'].setScale(STONE_SCALE if z_pos == z else 0)

        for i, line_node in enumerate(self.line_nodes):
            if i == z: line_node.show()
            else: line_node.hide()

    def cut_layer(self): # not properly implemented yet, right now it just shrinks the outer layers (stones get smaller).
        if self.x_size < 3 or self.y_size <= 3 or self.z_size <= 3: return
        biggestaxis = max(self.x_size, self.y_size, self.z_size)

        if self.layer_count < (biggestaxis / 2) - 1:
            for i in range(0, self.layer_count + 1):
                for node in self.nodes:
                    coords = node.getName().split('_')[1].split(',')
                    x, y, z = [int(c) for c in coords]
                    if (x <= i or y <= i or z <= i or
                        x >= self.x_size-1-i or y >= self.y_size-1-i or z >= self.z_size-1-i):
                        node.setScale(0.2)
                for pos, ball_data in list(self.balls.items()):
                    x, y, z = pos
                    if (x <= i or y <= i or z <= i or
                        x >= self.x_size-1-i or y >= self.y_size-1-i or z >= self.z_size-1-i):
                        ball_data['node'].setScale(0.2)
            self.layer_count += 1
        else:
            self.layer_count = 0
            self.show_everything()

    # ========================================== #
    # WEIQI / GO LOGIC (RULES & MECHANICS)       #
    # ========================================== #
    def spawn_model(self, pos):      # `pos` can come from a click or from typed coordinates, we turn it into an int tuple
        if self.game_ended: return
        pos_tuple = tuple(int(round(coord)) for coord in pos)
        if not self.is_legal_move(pos_tuple, self.current_color): return # legality: occupied, suicide, superko

        mover = self.current_color   # remember who is playing, current_color changes below

        # [FIX 5] Create the stone with the shared helper and register it in balls.
        self.balls[pos_tuple] = self.create_stone(pos_tuple, mover)

        # Captures: look at the neighbours of the new stone. Any opponent group left
        # without liberties is captured.
        opponent_color = 1 - mover
        captured_positions = set()
        for adj_pos in self.get_adjacent_positions(pos_tuple):
            if adj_pos in self.balls and self.balls[adj_pos]['color'] == opponent_color:
                group = self.get_group(adj_pos, opponent_color)
                if not self.group_has_liberty(group):
                    captured_positions.update(group)

        if captured_positions: # remove the captured stones and score them for the player who captured
            for captured_pos in captured_positions:
                if captured_pos in self.balls:
                    self.balls[captured_pos]['node'].removeNode()
                    del self.balls[captured_pos]
            if opponent_color == 0: self.white_captures += len(captured_positions)
            else: self.black_captures += len(captured_positions)
            self.text_captures.setText(f"Black captures: {self.black_captures}\nWhite captures: {self.white_captures}")

        if self.increment > 0: # timers with increment: the player who just played gets extra time
            if mover == 0: self.black_time += self.increment
            else: self.white_time += self.increment

        self.current_color = 1 - mover  # pass the turn
        self.turn += 1
        self.pass_count = 0  # someone played, so "two passes in a row" starts again from zero

        # [FIX 5][FIX 6] Save this position (also stores its hash for superko).
        self.record_state()

        self.turn_text.setText(f"Turn: {self.turn}")

        # [FIX 2] Timers. Before, the Byo-yomi reset was an `elif` after
        # `if timer_mode != "No Timer"`, so it could never run (Byo-yomi is also != "No Timer").
        self.reset_byo_yomi(mover)
        if self.timer_mode != "No Timer":
            self.pause_timer()
            self.start_timer()

    '''
    [FIX 2] Byo-yomi rule: when a player makes a move, THEIR current period clock starts over.
    The number of periods left is NOT refilled (a period is only lost when its clock runs out,
    update_timer does that). The old code refilled the periods on every move, which made
    them meaningless. If you liked the old behaviour, add
    `self.black_byo_yomi_periods = self.byo_periods` (and white) back in here.
    '''
    def reset_byo_yomi(self, color):
        if self.timer_mode != "Byo-yomi": return
        if color == 0:
            self.black_byo_yomi_time = self.byo_time
        else:
            self.white_byo_yomi_time = self.byo_time

    '''
    Checks the legality of a play. We simulate the move on a copy of the board:
      1. remove any opponent group the new stone captures,
      2. if our own group then has no liberties, it's suicide -> illegal,
      3. if the resulting position already happened before -> superko -> illegal.
    '''
    def is_legal_move(self, pos, color):
        pos_tuple = tuple(int(round(coord)) for coord in pos)
        if pos_tuple in self.balls: return False

        # Virtual execution setup: sim_board is {(x,y,z): color}
        sim_board = {k: v['color'] for k, v in self.balls.items()}
        sim_board[pos_tuple] = color

        opponent_color = 1 - color
        captured_positions = set()

        for adj_pos in self.get_adjacent_positions(pos_tuple):
            if (adj_pos in sim_board and sim_board[adj_pos] == opponent_color):
                group = self.get_virtual_group(adj_pos, opponent_color, sim_board)
                if not self.virtual_group_has_liberty(group, sim_board):
                    captured_positions.update(group)

        for captured_pos in captured_positions:
            del sim_board[captured_pos]

        # Suicide check on virtual board
        player_group = self.get_virtual_group(pos_tuple, color, sim_board)
        if not self.virtual_group_has_liberty(player_group, sim_board):
            return False

        # [FIX 3] Positional superko: compare against EVERY earlier position.
        # Before, only match_history[-2] was checked, which is just simple ko.
        board_hash = self.get_virtual_board_hash(sim_board)
        previous_positions = {state['board_hash'] for state in self.match_history}
        if board_hash in previous_positions:
            return False
        return True

    '''
    Given a position, returns the group of connected stones of `color` that contains it
    (on the virtual board used by is_legal_move).
    '''
    def get_virtual_group(self, pos, color, sim_board):
        visited = set()
        group = set()
        to_check = [pos]
        while to_check:
            current = to_check.pop()
            if current in visited or current not in sim_board or sim_board[current] != color: continue
            visited.add(current)
            group.add(current)
            to_check.extend(self.get_adjacent_positions(current))
        return group

    def virtual_group_has_liberty(self, group, sim_board):
        for pos in group:
            for adj in self.get_adjacent_positions(pos):
                if adj not in sim_board: return True
        return False

    def get_virtual_board_hash(self, sim_board):
        board_state = sorted((pos, col) for pos, col in sim_board.items())
        return hashlib.md5(str(board_state).encode()).hexdigest()

    def get_group(self, pos, color, visited=None):
        if visited is None: visited = set()
        if pos not in self.balls or self.balls[pos]['color'] != color or pos in visited: return set()

        group = set()
        to_check = [pos]
        while to_check:
            current = to_check.pop()
            if current in visited or current not in self.balls or self.balls[current]['color'] != color: continue
            visited.add(current)
            group.add(current)
            to_check.extend(self.get_adjacent_positions(current))
        return group

    '''
    Given a group, check if any of its stones has an empty neighbour (a liberty).
    '''
    def group_has_liberty(self, group):
        if not group: return False
        for pos in group:
            for adj in self.get_adjacent_positions(pos):
                if adj not in self.balls: return True
        return False

    '''
    Given a position, return its (up to 6) neighbours that are inside the board.
    '''
    def get_adjacent_positions(self, pos):
        x, y, z = pos
        adj = [(x+1,y,z), (x-1,y,z), (x,y+1,z), (x,y-1,z), (x,y,z+1), (x,y,z-1)]
        return [(ax, ay, az) for ax, ay, az in adj if 0 <= ax < self.x_size and 0 <= ay < self.y_size and 0 <= az < self.z_size]

    '''
    Hash of the current board (positions + colors). Used to detect superko.
    '''
    def get_board_hash(self):
        board_state = sorted((pos, self.balls[pos]['color']) for pos in self.balls)
        return hashlib.md5(str(board_state).encode()).hexdigest()

    def pass_turn(self):
        if self.game_ended: return
        passer = self.current_color      # who is passing
        self.pass_count += 1
        self.current_color = 1 - passer
        self.turn += 1
        self.turn_text.setText(f"Turn: {self.turn}")

        # [FIX 6] A pass is a turn too: save it so rewind and save/load know about it.
        # (The board doesn't change, so its hash equals the previous position's hash.)
        self.record_state()

        # [FIX 2] Only touch the timers if one is actually selected. Before, start_timer()
        # was called even with "No Timer", and the clock would end up declaring a timeout.
        self.reset_byo_yomi(passer)
        if self.timer_mode != "No Timer":
            self.start_timer()

        self.check_game_end()

    def check_game_end(self):
        if self.pass_count >= 2: self.end_game()

    def end_game(self):
        self.game_ended = True
        self.pause_timer()
        self.show_everything()
        self.points()
        winner = "Black" if self.black_points > self.white_points else "White" if self.white_points > self.black_points else "Draw"
        self.clear_game_over_text()   # never stack two texts on top of each other
        self.game_over_text = OnscreenText(
            text=f"Game Over! {winner} wins!",
            pos=(0, 0), scale=0.1, fg=(0, 1, 0, 1), align=TextNode.ACenter, font=self.custom_font
        )

    def points(self):
        self.black_points = self.black_captures + self.calculate_territory(0)
        self.white_points = self.white_captures + self.calculate_territory(1) + self.komi
        self.text_points.setText(text=f"Black: {self.black_points} pts\nWhite: {self.white_points} pts")
        self.text_captures.setText(text=f"Black cap: {self.black_captures}\nWhite cap: {self.white_captures}")

    def calculate_territory(self, color):
        territory, visited = 0, set()
        for x in range(self.x_size):
            for y in range(self.y_size):
                for z in range(self.z_size):
                    pos = (x, y, z)
                    if pos not in self.balls and pos not in visited:
                        region = self.get_region(pos, visited)

                        colors_touched = set()
                        for reg_pos in region:
                            for adj in self.get_adjacent_positions(reg_pos):
                                if adj in self.balls:
                                    colors_touched.add(self.balls[adj]['color'])

                        # Territory = an empty region touched by stones of one color only
                        if len(colors_touched) == 1 and color in colors_touched:
                            territory += len(region)
        return territory

    def get_region(self, pos, visited):
        if pos in self.balls or pos in visited: return set()
        region, to_check = set(), [pos]
        while to_check:
            current = to_check.pop()
            if current in visited or current in self.balls: continue
            visited.add(current)
            region.add(current)
            to_check.extend([adj for adj in self.get_adjacent_positions(current) if adj not in self.balls and adj not in visited])
        return region

    '''
    Undo the last turn (a move or a pass).
    [FIX 5][FIX 6] The previous snapshot is restored with restore_state, which also resets
    pass_count, game_ended and the "Game Over" text. If we rewind past the first turn
    we go back to the empty board.
    '''
    def rewind_turn(self):
        if not self.match_history: return
        was_ended = self.game_ended

        self.match_history.pop()
        if self.match_history:
            self.restore_state(self.match_history[-1])
        else:
            self.restore_state(self.empty_state())

        # The game was over (timer was paused by end_game): play continues, so the clock too.
        if was_ended and self.timer_mode != "No Timer":
            self.start_timer()

    # ========================================== #
    # TIMER SYSTEM                               #
    # ========================================== #
    def set_timer_mode(self, mode):
        self.timer_mode = mode
        self.reset_timers()

    def set_main_time(self, text):
        try: self.main_time = int(text); self.reset_timers()
        except ValueError: pass

    def set_increment(self, text):
        try: self.increment = int(text)
        except ValueError: pass

    def set_byo_time(self, text):
        try: self.byo_time = int(text); self.reset_timers()
        except ValueError: pass

    def set_byo_periods(self, text):
        try: self.byo_periods = int(text); self.reset_timers()
        except ValueError: pass

    def reset_timers(self):
        if self.timer_mode != "No Timer":
            self.black_time = self.main_time
            self.white_time = self.main_time
            self.black_byo_periods_left = self.byo_periods
            self.white_byo_periods_left = self.byo_periods
            self.timer_running = False
            self.update_timer_display()

    def start_timer(self):
        if self.timer_task: self.taskMgr.remove(self.timer_task)
        self.timer_running = True
        self.timer_task = self.taskMgr.add(self.update_timer, "update_timer")

    def pause_timer(self):
        if self.timer_task: self.taskMgr.remove(self.timer_task)
        self.timer_running = False

    def update_timer(self, task):
        if not self.timer_running or self.game_ended: return task.cont
        dt = globalClock.getDt()   # seconds since the last frame

        # Work on the clock of the player whose turn it is.
        c_time = self.black_time if self.current_color == 0 else self.white_time
        c_byo_time = self.black_byo_yomi_time if self.current_color == 0 else self.white_byo_yomi_time
        c_byo_per = self.black_byo_yomi_periods if self.current_color == 0 else self.white_byo_yomi_periods

        if c_time > 0:
            c_time = max(0, c_time - dt)      # main time first
        else:
            c_byo_time -= dt                  # then the byo-yomi period
            if c_byo_time <= 0:
                c_byo_per -= 1                # period used up
                if c_byo_per <= 0:
                    self.handle_timeout()
                    return task.done
                c_byo_time = self.byo_time    # next period starts

        if self.current_color == 0:
            self.black_time, self.black_byo_yomi_time, self.black_byo_yomi_periods = c_time, c_byo_time, c_byo_per
        else:
            self.white_time, self.white_byo_yomi_time, self.white_byo_yomi_periods = c_time, c_byo_time, c_byo_per

        b_disp = self.format_time(self.black_time, self.black_byo_yomi_time, self.black_byo_yomi_periods)
        w_disp = self.format_time(self.white_time, self.white_byo_yomi_time, self.white_byo_yomi_periods)
        self.timer_text.setText(f"Black: {b_disp}\nWhite: {w_disp}")
        return task.cont

    def format_time(self, main_time, byo_yomi_time, byo_yomi_periods):
        if main_time > 0:
            return f"{int(main_time // 60)}:{int(main_time % 60):02d}"
        return f"{int(byo_yomi_time)} ({byo_yomi_periods})"

    def handle_timeout(self):
        winner = "Black" if self.current_color == 1 else "White"
        self.game_ended = True
        self.timer_text.setText(f"{winner} wins by timeout!")
        self.clear_game_over_text()
        self.game_over_text = OnscreenText(
            text=f"Game Over! {winner} wins by timeout!",
            pos=(0, 0), scale=0.1, fg=(0.25, 1, 0, 1), align=TextNode.ACenter
        )

    def update_timer_display(self):
        if self.timer_mode == "No Timer":
            self.timer_text.setText("Black:NT\nWhite:NT")
            return
        b_mins, b_secs = divmod(int(self.black_time), 60)
        w_mins, w_secs = divmod(int(self.white_time), 60)
        b_ex = f" ({self.black_byo_periods_left} p)" if self.timer_mode == "Byo-yomi" else ""
        w_ex = f" ({self.white_byo_periods_left} p)" if self.timer_mode == "Byo-yomi" else ""
        self.timer_text.setText(f"Black: {b_mins}:{b_secs:02d}{b_ex}\nWhite: {w_mins}:{w_secs:02d}{w_ex}")

    # ========================================== #
    # INPUT & CAMERA CONTROLS                    #
    # ========================================== #
    def setup_collision_system(self):
        # A ray that starts at the camera and goes through the mouse; whatever grid point it hits is the one clicked.
        self.cTrav = CollisionTraverser()
        self.pickerQueue = CollisionHandlerQueue()
        self.pickerNode = CollisionNode("mouseRay")
        self.pickerNP = self.camera.attachNewNode(self.pickerNode)
        self.pickerRay = CollisionRay()
        self.pickerNode.addSolid(self.pickerRay)
        self.pickerNode.setFromCollideMask(1)
        self.cTrav.addCollider(self.pickerNP, self.pickerQueue)

    def bind_inputs(self):
        # self.accept("event", function): when that key/mouse event happens, Panda3D calls the function.
        self.accept("space", self.camera_control.start_rotation)
        self.accept("space-up", self.camera_control.stop_rotation)
        self.accept("wheel_up", self.camera_control.zoom_in)
        self.accept("wheel_down", self.camera_control.zoom_out)
        self.accept("arrow_left", self.camera_control.rotate_left)
        self.accept("arrow_right", self.camera_control.rotate_right)
        self.accept("arrow_up", self.camera_control.rotate_up)
        self.accept("arrow_down", self.camera_control.rotate_down)
        self.accept("s", self.cut_layer)

        self.accept("a", self.points)
        self.accept("x", self.gui_help)
        self.accept("c", self.gui_extra)
        self.accept("d", self.rewind_turn)
        self.accept("z", self.pass_turn)
        self.accept("mouse1", self.check_click)
        self.accept("mouse2", self.change_camera_center)
        self.accept("mouse3", self.camera_control.start_rotation)
        self.accept("mouse3-up", self.camera_control.stop_rotation)
        self.accept("r", self.call_reset_camera)
        self.accept("q", self.plane_down)
        self.accept("e", self.plane_up)
        self.accept("w", self.show_everything)

    def check_click(self):
        if not self.mouseWatcherNode.hasMouse(): return
        mpos = self.mouseWatcherNode.getMouse()
        self.pickerRay.setFromLens(self.camNode, mpos.getX(), mpos.getY())
        if self.x_size == 1 and self.y_size == 1 and self.z_size == 1: return

        self.cTrav.traverse(self.render)
        if self.pickerQueue.getNumEntries() > 0:
            self.pickerQueue.sortEntries()
            hit = self.pickerQueue.getEntry(0)   # closest hit
            pos = hit.getIntoNodePath().getParent().getPos(self.render)   # the collision node's parent is the vertex node
            pos_tuple = (int(round(pos.x)), int(round(pos.y)), int(round(pos.z)))

            if not (0 <= pos_tuple[0] < self.x_size and 0 <= pos_tuple[1] < self.y_size and 0 <= pos_tuple[2] < self.z_size): return
            if pos_tuple in self.balls: return
            self.spawn_model(pos_tuple)

    def call_reset_camera(self):
        self.reset_camera(self.size)

    def change_camera_center(self):
            if not self.mouseWatcherNode.hasMouse(): return
            mpos = self.mouseWatcherNode.getMouse()
            self.pickerRay.setFromLens(self.camNode, mpos.getX(), mpos.getY())

            self.cTrav.traverse(self.render)
            if self.pickerQueue.getNumEntries() > 0:
                self.pickerQueue.sortEntries()
                hit = self.pickerQueue.getEntry(0)

                # Position of the clicked grid point
                pos = hit.getIntoNodePath().getParent().getPos(self.render)
                pos_tuple = (int(round(pos.x)), int(round(pos.y)), int(round(pos.z)))

                # Make that point the new pivot the camera orbits around
                self.camera_control.center = Point3(pos_tuple[0], pos_tuple[1], pos_tuple[2])
                self.camera_control.update_camera_position()
                self.camera.lookAt(self.camera_control.center)

    def reset_camera(self, size):
        parts = [int(x.strip()) for x in size.split(',')]
        center = Point3((parts[0] - 1) / 2, (parts[1] - 1) / 2, (parts[2] - 1) / 2)
        self.camera_control.center = center
        self.camera_control.update_camera_position()
        self.camera.lookAt(center)

    # ========================================== #
    # USER INTERFACE (GUI)                       #
    # ========================================== #
    def process_coordinates(self, input_text=None):
        text = self.coord_entry.get() if input_text is None or not isinstance(input_text, str) else input_text
        try:
            coords = [int(x.strip()) for x in text.split(',')]
            if len(coords) != 3: return
            if not (0 <= coords[0] < self.x_size and 0 <= coords[1] < self.y_size and 0 <= coords[2] < self.z_size): return
            if tuple(coords) in self.balls: return
            self.spawn_model(tuple(coords))
            self.coord_entry["focus"] = 0
        except ValueError: return

    def set_grid_size(self, input_text=None):
        text = self.size_entry.get() if input_text is None else str(input_text)
        try:
            parts = [int(x.strip()) for x in text.split(',')]
            # "10" -> 10,10,10   |   "19,19" -> 19,19,1   |   "10,10,2" -> as typed
            if len(parts) == 1:
                normalized_size = f"{parts[0]},{parts[0]},{parts[0]}"
            elif len(parts) == 2:
                normalized_size = f"{parts[0]},{parts[1]},1"
            else:
                normalized_size = f"{parts[0]},{parts[1]},{parts[2]}"

            self.size = normalized_size
            self.reset_game()
            self.generate_grid(self.size)

            if self.timer_mode != "No Timer":
                self.reset_timers()
                self.pause_timer()
            self.size_entry["focus"] = 0
        except Exception:
            return

    def set_komi(self, text):
        try: self.komi = float(text)
        except ValueError: pass

    def setup_gui(self):
        if not self.custom_font.isValid(): self.custom_font = None
        TS = 0.065   # text size
        BS = 0.05    # button / entry size

        self.extra = OnscreenText(text="C: timer & settings", parent=self.a2dTopLeft, pos=(0.03, -0.07), scale=TS, align=TextNode.ALeft, font=self.custom_font)
        self.help = OnscreenText(text="X: help", parent=self.a2dTopLeft, pos=(0.03, -0.16), scale=TS, align=TextNode.ALeft, font=self.custom_font)
        self.timer_text = OnscreenText(text="Black: --:--\nWhite: --:--", parent=self.a2dTopLeft, pos=(0.03, -0.27), scale=TS, align=TextNode.ALeft, font=self.custom_font, mayChange=True)
        self.turn_text = OnscreenText(text=f"Turn: {self.turn}", parent=self.a2dTopLeft, pos=(0.03, -0.42), scale=TS, align=TextNode.ALeft, mayChange=True)

        self.text_points = OnscreenText(text=f"Black: {self.black_points} pts\nWhite: {self.white_points} pts", parent=self.a2dTopRight, pos=(-0.03, -0.07), scale=TS, align=TextNode.ARight, font=self.custom_font, mayChange=True)
        self.text_captures = OnscreenText(text=f"Black cap: {self.black_captures}\nWhite cap: {self.white_captures}", parent=self.a2dTopRight, pos=(-0.03, -0.25), scale=TS, align=TextNode.ARight, font=self.custom_font, mayChange=True)

        self.save_game_gui = DirectButton(text=("Save", "Saved!", "Save", "disabled"), parent=self.a2dTopRight, pos=(-0.15, 0, -0.42), scale=BS, command=self.save_game)
        self.load_game_gui = DirectButton(text=("Load", "Loaded!", "Load", "disabled"), parent=self.a2dTopRight, pos=(-0.15, 0, -0.52), scale=BS, command=self.load_game)

        self.coord_label_info = OnscreenText(text="Blue ball = (0,0,0)", parent=self.a2dTopCenter, pos=(0, -0.07), scale=0.06, align=TextNode.ACenter, font=self.custom_font)

        self.size_label = OnscreenText(text="Grid size (x,y,z):", parent=self.a2dBottomLeft, pos=(0.03, 0.15), scale=TS, align=TextNode.ALeft, font=self.custom_font)
        self.size_entry = DirectEntry(text="", scale=BS, command=self.set_grid_size, initialText="", numLines=1, focus=0, parent=self.a2dBottomLeft, pos=(0.04, 0, 0.05), frameColor=(0, 0, 0, 0.6), text_fg=(1, 1, 1, 1), width=9)

        self.coord_label = OnscreenText(text="Move (x,y,z):", parent=self.a2dBottomCenter, pos=(0, 0.15), scale=TS, align=TextNode.ACenter, font=self.custom_font)
        self.coord_entry = DirectEntry(text="", scale=BS, command=self.process_coordinates, initialText="", numLines=1, focus=0, parent=self.a2dBottomCenter, pos=(-0.20, 0, 0.05), frameColor=(0, 0, 0, 0.6), text_fg=(1, 1, 1, 1), width=8)
        self.coord_entry.bind(DGG.ENTER, self.process_coordinates)

    def gui_help(self):
        if not self.custom_font.isValid(): self.custom_font = None
        if self.help_show == 0:
            self.hotkeys = OnscreenText(
                text="Space+mouse: rotate  |  LClick: play  |  RClick: recenter\n"
                     "R: reset camera  |  E/Q: plane up/down  |  W: show all\n"
                     "A: count points  |  Z: pass  |  D: rewind  |  S: shrink layer",
                parent=self.a2dTopLeft, pos=(0.03, -0.58), scale=0.065, align=TextNode.ALeft, font=self.custom_font
            )
        else: self.hotkeys.destroy()
        self.help_show = 1 - self.help_show

    def gui_extra(self):
        # Toggles the settings panel (colors, komi, timer). Open: build every row. Closed: destroy them.
        if self.extra_show == 0:
            S, ES, Ex = 0.065, 0.05, -0.70   # label size, entry size, x position of the entries
            # (y position, label, key, command, initial text, entry width)
            rows = [
                (-0.22, "Grid color (r,g,b)",  "color",      self.custom_grid_color,     "",    6),
                (-0.33, "Bg color (r,g,b)",    "background", self.custom_background_color,"",   6),
                (-0.44, "Komi",                "komi",       self.set_komi,              "",    4),
                (-0.55, "Timer method",        "timer_mode", None,                       None,  0),
                (-0.66, "Main time (s)",       "main_time",  self.set_main_time,         "300", 4),
                (-0.77, "Increment (s)",       "increment",  self.set_increment,         "0",   4),
                (-0.88, "Byo-yomi time (s)",   "byo_time",   self.set_byo_time,          "30",  4),
                (-0.95, "Byo-yomi periods",    "byo_periods",self.set_byo_periods,       "5",   4),
            ]
            for y, label, key, cmd, init, w in rows:
                self.extra_gui_elements[f"{key}_label"] = OnscreenText(text=label, parent=self.a2dRightCenter, pos=(-0.03, y), scale=S, align=TextNode.ARight, font=self.custom_font)
                if key == "timer_mode":
                    self.extra_gui_elements["timer_mode_menu"] = DirectOptionMenu(parent=self.a2dRightCenter, pos=(Ex, 0, y), scale=ES, items=["No Timer", "Absolute", "Fischer", "Byo-yomi"], command=self.set_timer_mode, initialitem=0, frameColor=(0, 0, 0, 0.5), text_fg=(1, 1, 1, 1))
                else:
                    entry = DirectEntry(text="", parent=self.a2dRightCenter, pos=(Ex, 0, y), scale=ES, command=cmd, initialText=init, frameColor=(0, 0, 0, 0.5), text_fg=(1, 1, 1, 1), width=w)
                    self.extra_gui_elements[f"{key}_entry"] = entry

            # Pressing Enter inside any of these entries calls its command with the typed text.
            for name in ["color_entry", "background_entry", "main_time_entry", "increment_entry", "byo_time_entry", "byo_periods_entry"]:
                if name in self.extra_gui_elements:
                    self.extra_gui_elements[name].bind(DGG.ENTER, lambda event, n=name: self.extra_gui_elements[n]["command"](self.extra_gui_elements[n].get()))
        else:
            for element in self.extra_gui_elements.values():
                if element is not None: element.destroy()
            self.extra_gui_elements.clear()
        self.extra_show = 1 - self.extra_show


# [FIX 5] Guard so the file can be imported (e.g. to test the rules) without opening the game window.
if __name__ == "__main__":
    app = GridDemo(0)
    app.run()
