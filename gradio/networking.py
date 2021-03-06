"""
Defines helper methods useful for setting up ports, launching servers, and handling `ngrok`
"""

import os
import socket
import threading
from flask import Flask, request, jsonify, abort, send_file, render_template
from flask_cachebuster import CacheBuster
from flask_cors import CORS
import threading
import pkg_resources
from distutils import dir_util
import time
import json
import urllib.request
from shutil import copyfile
import requests
import sys
import csv
import logging
import gradio as gr
from gradio.embeddings import calculate_similarity, fit_pca_to_embeddings, transform_with_pca
from gradio.tunneling import create_tunnel

INITIAL_PORT_VALUE = int(os.getenv(
    'GRADIO_SERVER_PORT', "7860"))  # The http server will try to open on port 7860. If not available, 7861, 7862, etc.
TRY_NUM_PORTS = int(os.getenv(
    'GRADIO_NUM_PORTS', "100"))  # Number of ports to try before giving up and throwing an exception.
LOCALHOST_NAME = os.getenv(
    'GRADIO_SERVER_NAME', "127.0.0.1")
GRADIO_API_SERVER = "https://api.gradio.app/v1/tunnel-request"
GRADIO_FEATURE_ANALYTICS_URL = "https://api.gradio.app/gradio-feature-analytics/"

STATIC_TEMPLATE_LIB = pkg_resources.resource_filename("gradio", "templates/")
STATIC_PATH_LIB = pkg_resources.resource_filename("gradio", "static/")
GRADIO_STATIC_ROOT = "https://gradio.app"

app = Flask(__name__,
    template_folder=STATIC_TEMPLATE_LIB,
    static_folder=STATIC_PATH_LIB,
    static_url_path="/static/")
CORS(app)
cache_buster = CacheBuster(config={'extensions': ['.js', '.css'], 'hash_size': 5})
cache_buster.init_app(app)
app.app_globals = {}

# Hide Flask default message
cli = sys.modules['flask.cli']
cli.show_server_banner = lambda *x: None

def set_meta_tags(title, description, thumbnail):
    app.app_globals.update({
        "title": title,
        "description": description,
        "thumbnail": thumbnail
    })


def set_config(config):
    app.app_globals["config"] = config


def get_local_ip_address():
    try:
        ip_address = requests.get('https://api.ipify.org').text
    except requests.ConnectionError:
        ip_address = "No internet connection"
    return ip_address

IP_ADDRESS = get_local_ip_address()

def get_first_available_port(initial, final):
    """
    Gets the first open port in a specified range of port numbers
    :param initial: the initial value in the range of port numbers
    :param final: final (exclusive) value in the range of port numbers, should be greater than `initial`
    :return:
    """
    for port in range(initial, final):
        try:
            s = socket.socket()  # create a socket object
            s.bind((LOCALHOST_NAME, port))  # Bind to the port
            s.close()
            return port
        except OSError:
            pass
    raise OSError(
        "All ports from {} to {} are in use. Please close a port.".format(
            initial, final
        )
    )


@app.route("/", methods=["GET"])
def main():
    return render_template("index.html",
        title=app.app_globals["title"],
        description=app.app_globals["description"],
        thumbnail=app.app_globals["thumbnail"],
        vendor_prefix=(GRADIO_STATIC_ROOT if app.interface.share else "")
    )


@app.route("/config/", methods=["GET"])
def config():
    return jsonify(app.app_globals["config"])


@app.route("/enable_sharing/<path:path>", methods=["GET"])
def enable_sharing(path):
    if path == "None":
        path = None
    app.app_globals["config"]["share_url"] = path
    return jsonify(success=True)
    

@app.route("/api/predict/", methods=["POST"])
def predict():
    raw_input = request.json["data"]
    prediction, durations = app.interface.process(raw_input)
    output = {"data": prediction, "durations": durations}
    return jsonify(output)

def log_feature_analytics(feature):
    if app.interface.analytics_enabled:
        try:
            requests.post(GRADIO_FEATURE_ANALYTICS_URL, 
            data={
                'ip_address': IP_ADDRESS,
                'feature': feature})
        except requests.ConnectionError:
            pass  # do not push analytics if no network

@app.route("/api/score_similarity/", methods=["POST"])
def score_similarity():
    raw_input = request.json["data"]

    preprocessed_input = [input_interface.preprocess(raw_input[i])
                    for i, input_interface in enumerate(app.interface.input_interfaces)]
    input_embedding = app.interface.embed(preprocessed_input)
    scores = list()

    for example in app.interface.examples:
        preprocessed_example = [iface.preprocess(iface.preprocess_example(example))
            for iface, example in zip(app.interface.input_interfaces, example)]
        example_embedding = app.interface.embed(preprocessed_example)
        scores.append(calculate_similarity(input_embedding, example_embedding))    
    log_feature_analytics('score_similarity')
    return jsonify({"data": scores})


@app.route("/api/view_embeddings/", methods=["POST"])
def view_embeddings():    
    sample_embedding = []
    if "data" in request.json:
        raw_input = request.json["data"]
        preprocessed_input = [input_interface.preprocess(raw_input[i])
                        for i, input_interface in enumerate(app.interface.input_interfaces)]
        sample_embedding.append(app.interface.embed(preprocessed_input))

    example_embeddings = []
    for example in app.interface.examples:
        preprocessed_example = [iface.preprocess(iface.preprocess_example(example))
            for iface, example in zip(app.interface.input_interfaces, example)]
        example_embedding = app.interface.embed(preprocessed_example)
        example_embeddings.append(example_embedding)
    
    pca_model, embeddings_2d = fit_pca_to_embeddings(sample_embedding + example_embeddings)
    sample_embedding_2d = embeddings_2d[:len(sample_embedding)]
    example_embeddings_2d = embeddings_2d[len(sample_embedding):]
    app.pca_model = pca_model
    log_feature_analytics('view_embeddings')
    return jsonify({"sample_embedding_2d": sample_embedding_2d, "example_embeddings_2d": example_embeddings_2d})


@app.route("/api/update_embeddings/", methods=["POST"])
def update_embeddings():    
    sample_embedding, sample_embedding_2d = [], []
    if "data" in request.json:
        raw_input = request.json["data"]
        preprocessed_input = [input_interface.preprocess(raw_input[i])
                        for i, input_interface in enumerate(app.interface.input_interfaces)]
        sample_embedding.append(app.interface.embed(preprocessed_input))
        sample_embedding_2d = transform_with_pca(app.pca_model, sample_embedding)
    
    return jsonify({"sample_embedding_2d": sample_embedding_2d})


@app.route("/api/predict_examples/", methods=["POST"])
def predict_examples():
    example_ids = request.json["data"]
    predictions_set = {}
    for example_id in example_ids:
        example_set = app.interface.examples[example_id]
        processed_example_set = [iface.preprocess_example(example)
            for iface, example in zip(app.interface.input_interfaces, example_set)]
        try:
            predictions, _ = app.interface.process(processed_example_set)
        except:
            continue
        predictions_set[example_id] = predictions
    output = {"data": predictions_set}
    return jsonify(output)


@app.route("/api/flag/", methods=["POST"])
def flag():
    log_feature_analytics('flag')
    flag_path = os.path.join(app.cwd, app.interface.flagging_dir)
    os.makedirs(flag_path,
                exist_ok=True)
    output = {'inputs': [app.interface.input_interfaces[
        i].rebuild(
        flag_path, request.json['data']['input_data'][i]) for i
        in range(len(app.interface.input_interfaces))],
        'outputs': [app.interface.output_interfaces[
            i].rebuild(
            flag_path, request.json['data']['output_data'][i])
            for i
        in range(len(app.interface.output_interfaces))]}

    log_fp = "{}/log.csv".format(flag_path)

    is_new = not os.path.exists(log_fp)

    with open(log_fp, "a") as csvfile:
        headers = ["input_{}".format(i) for i in range(len(
            output["inputs"]))] + ["output_{}".format(i) for i in
                                    range(len(output["outputs"]))]
        writer = csv.DictWriter(csvfile, delimiter=',',
                                lineterminator='\n',
                                fieldnames=headers)
        if is_new:
            writer.writeheader()

        writer.writerow(
            dict(zip(headers, output["inputs"] +
                        output["outputs"]))
        )
        return jsonify(success=True)


@app.route("/api/interpret/", methods=["POST"])
def interpret():
    log_feature_analytics('interpret')
    raw_input = request.json["data"]
    interpretation_scores, alternative_outputs = app.interface.interpret(raw_input)
    return jsonify({
        "interpretation_scores": interpretation_scores,
        "alternative_outputs": alternative_outputs
    })


@app.route("/file/<path:path>", methods=["GET"])
def file(path):
    return send_file(os.path.join(app.cwd, path))

def start_server(interface, server_name, server_port=None):
    if server_port is None:
        server_port = INITIAL_PORT_VALUE
    port = get_first_available_port(
        server_port, server_port + TRY_NUM_PORTS
    )
    app.interface = interface
    app.cwd = os.getcwd()
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    if interface.save_to is not None:
        interface.save_to["port"] = port
    thread = threading.Thread(target=app.run,
                              kwargs={"port": port, "host": server_name},
                              daemon=True)
    thread.start()
    return port, app, thread

def close_server(process):
    process.terminate()
    process.join()

def url_request(url):
    try:
        req = urllib.request.Request(
            url=url, headers={"content-type": "application/json"}
        )
        res = urllib.request.urlopen(req, timeout=10)
        return res
    except Exception as e:
        raise RuntimeError(str(e))


def setup_tunnel(local_server_port):
    response = url_request(GRADIO_API_SERVER)
    if response and response.code == 200:
        try:
            payload = json.loads(response.read().decode("utf-8"))[0]
            return create_tunnel(payload, LOCALHOST_NAME, local_server_port)

        except Exception as e:
            raise RuntimeError(str(e))


def url_ok(url):
    try:
        r = requests.head(url)
        return r.status_code == 200
    except ConnectionError:
        return False
