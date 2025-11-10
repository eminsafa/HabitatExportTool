import base64
import os
import re
from io import BytesIO

import habitat_sim
import numpy as np
import quaternion
from PIL import Image
from flask import Flask, request, jsonify, render_template

SCENE_FILE = "/path/to/your/Replica/apartment_0/habitat/mesh_semantic.ply"
CAMERA_PARAMS = {"w": 1200, "h": 680, "fx": 600.0, "fy": 600.0, "cx": 599.5, "cy": 339.5, "scale": 6553.5}
DATA_DIR = "data/results"
TRAJ_DIR = "data"
sim = None
agent = None


def get_rotation(yaw_degrees):
    yaw_rad = np.deg2rad(yaw_degrees)
    return quaternion.from_euler_angles(0, yaw_rad, 0).normalized()


def pose_to_4x4_matrix(position, rotation_quat):
    """Converts agent position and rotation to a 4x4 transformation matrix."""
    rot_matrix = quaternion.as_rotation_matrix(rotation_quat)
    transform_matrix = np.eye(4)
    transform_matrix[:3, :3] = rot_matrix
    transform_matrix[:3, 3] = position
    return transform_matrix


def format_number_for_traj(num):
    """Replicates the specific scientific notation format from the JavaScript code."""
    if num == 0.0:
        return "0.000000000000000000e+00"

    # Format to scientific notation with 18 decimal places
    exp_str = "{:.18e}".format(num)

    # Pad the exponent to two digits with a sign
    match = re.match(r"(-?\d+\.\d+e)([+-])(\d+)", exp_str)
    if match:
        mantissa, sign, exponent = match.groups()
        return f"{mantissa}{sign}{int(exponent):02d}"
    return exp_str  # Fallback


def pose_to_4x4_matrix_string(matrix):
    """Converts a 4x4 numpy matrix to the required space-separated string format."""
    return ' '.join(format_number_for_traj(n) for n in matrix.flatten())


def make_camera_cfg(params, include_depth=False):
    rgb_sensor_spec = habitat_sim.CameraSensorSpec()
    rgb_sensor_spec.uuid = "color_sensor"
    rgb_sensor_spec.sensor_type = habitat_sim.SensorType.COLOR
    rgb_sensor_spec.resolution = [params["h"], params["w"]]
    rgb_sensor_spec.hfov = np.rad2deg(2 * np.arctan(params["w"] / (2 * params["fx"])))
    sensor_specs = [rgb_sensor_spec]

    if include_depth:
        depth_sensor_spec = habitat_sim.CameraSensorSpec()
        depth_sensor_spec.uuid = "depth_sensor"
        depth_sensor_spec.sensor_type = habitat_sim.SensorType.DEPTH
        depth_sensor_spec.resolution = [params["h"], params["w"]]
        depth_sensor_spec.hfov = rgb_sensor_spec.hfov
        sensor_specs.append(depth_sensor_spec)
    return sensor_specs


def make_simulator_cfg(scene_path, camera_params):
    sim_cfg = habitat_sim.SimulatorConfiguration()
    sim_cfg.gpu_device_id = 0
    sim_cfg.scene_id = scene_path
    sim_cfg.enable_physics = False
    agent_cfg = habitat_sim.agent.AgentConfiguration()
    agent_cfg.sensor_specifications = make_camera_cfg(camera_params, include_depth=True)
    return habitat_sim.Configuration(sim_cfg, [agent_cfg])


def get_sim_and_agent():
    global sim, agent
    if sim is None:
        print("--- Initializing Habitat Simulator (once) ---")
        if not os.path.exists(SCENE_FILE):
            print(f"ERROR: Scene file not found at '{SCENE_FILE}'")
            raise FileNotFoundError(f"Scene file not found: {SCENE_FILE}")
        cfg = make_simulator_cfg(SCENE_FILE, CAMERA_PARAMS)
        sim = habitat_sim.Simulator(cfg)
        agent = sim.get_agent(0)
        print("--- Simulator Ready ---")
    return sim, agent


app = Flask(__name__)


def get_observation_at(sim, agent, position, rotation_quat):
    agent_state = habitat_sim.AgentState(position, rotation_quat)
    agent.set_state(agent_state, reset_sensors=False)
    return sim.get_sensor_observations()


def encode_image_for_web(image_array):
    pil_img = Image.fromarray(image_array[..., :3], 'RGB')
    buffer = BytesIO()
    pil_img.save(buffer, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("utf-8")


# --- API Endpoints ---
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/control", methods=['POST'])
def control():
    sim, agent = get_sim_and_agent()
    data = request.json
    actions = data.get('actions', [])
    pos, yaw = np.array(data['position']), data['yaw']
    move_amount, turn_amount = 0.1, 5.0

    for action in actions:
        if action == 'turn_left':
            yaw += turn_amount
        elif action == 'turn_right':
            yaw -= turn_amount

    yaw_rad = np.deg2rad(-yaw)
    direction = np.array([np.sin(yaw_rad), 0, -np.cos(yaw_rad)])

    for action in actions:
        if action == 'move_forward':
            pos += direction * move_amount
        elif action == 'move_backward':
            pos -= direction * move_amount
        elif action == 'strafe_left':
            pos += np.array([direction[2], 0, direction[0]]) * move_amount
        elif action == 'strafe_right':
            pos += np.array([-direction[2], 0, -direction[0]]) * move_amount
        elif action == 'move_up':
            pos[1] += move_amount / 2
        elif action == 'move_down':
            pos[1] -= move_amount / 2

    rotation = get_rotation(yaw)
    observations = get_observation_at(sim, agent, pos, rotation)
    rgb_image = observations["color_sensor"]

    if data.get('save', False):
        os.makedirs(DATA_DIR, exist_ok=True)
        frame_index = data.get('frame_index', 1)

        rgb_pil = Image.fromarray(rgb_image[..., :3], 'RGB')
        rgb_pil.save(os.path.join(DATA_DIR, f"frame{frame_index:06d}.jpg"), "JPEG")

        depth_data = observations["depth_sensor"]
        depth_scaled = (depth_data * CAMERA_PARAMS["scale"]).astype(np.uint16)
        depth_pil = Image.fromarray(depth_scaled, mode='I;16')
        depth_pil.save(os.path.join(DATA_DIR, f"depth{frame_index:06d}.png"))

        transform_matrix = pose_to_4x4_matrix(pos, rotation)
        pose_string = pose_to_4x4_matrix_string(transform_matrix)
        traj_path = os.path.join(TRAJ_DIR, "traj.txt")

        is_first = data.get('is_first_frame', False)
        file_mode = 'w' if is_first else 'a'

        with open(traj_path, file_mode) as f:
            f.write(pose_string + '\n')

    return jsonify({'image': encode_image_for_web(rgb_image), 'position': pos.tolist(), 'yaw': yaw})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=False)