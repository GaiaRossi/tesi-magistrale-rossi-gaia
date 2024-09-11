from flask import Flask
import threading, time

app = Flask(__name__)

mock_odte = 0.9
lock = threading.Lock()

@app.route('/metrics')
def simulate_value():
    global mock_odte
    with lock:
        data = f"odte[pt=\"dt-1\"] {str(mock_odte)}"
    data = data.replace("[", "{").replace("]", "}")
    return data

def reduce_odte():
    global mock_odte
    time.sleep(120)
    with lock:
        mock_odte = 0.1

if __name__ == '__main__':
	t = threading.Thread(target=reduce_odte, daemon=True)
	t.start()
	app.run(host='0.0.0.0', port=8000, debug=False)