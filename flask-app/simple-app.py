from flask import Flask
import random

app = Flask(__name__)

mock_odte = 0.9

@app.route('/metrics')
def simulate_value():
    global mock_odte
    data = f"odte[pt=\"dt-1\"] {str(mock_odte)}"
    data = data.replace("[", "{").replace("]", "}")
    return data


@app.route('/disentangle')
def simulate_disentaglement():
      global mock_odte
      mock_odte = 0.1
      return {"message": "Set to 0.1"}

@app.route('/entangle')
def simulate_entaglement():
      global mock_odte
      mock_odte = 1.0
      return {"message": "Set to 1.0"}

if __name__ == '__main__':
	app.run(host='0.0.0.0', port=8000)