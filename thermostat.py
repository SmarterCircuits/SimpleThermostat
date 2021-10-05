import Adafruit_DHT
import RPi.GPIO as GPIO
import time
import requests
import json
from datetime import datetime, timedelta
import socket
import os
import paho.mqtt.client as mqtt

file_logging = True
myname = socket.gethostname()

room = myname.replace("thermopi","")
settings = {}
settings["failed_read_halt_limit"] = 10
settings["temperature_high_setting"] = 73
settings["temperature_low_setting"] = 69
settings["humidity_setting"] = 50
settings["air_circulation_minutes"] = 30
settings["circulation_cycle_minutes"] = 10
settings["ventilation_cycle_minutes"] = 10
settings["stage_limit_minutes"] = 15
settings["stage_cooldown_minutes"] = 5
settings["use_whole_house_fan"] = 0
settings["system_disabled"] = 0
settings["swing_temp_offset"] = 1
extra_ventilation_circuits = []
extra_circulation_circuits = []
humidification_circuits = []
log_loc = "/home/pi/"
broker_ip = "192.168.1.200"

post = True
temperature = None
humidity = None
failed_reads = 0
last_circulation = datetime.now()
circulate_until = datetime.now() + timedelta(minutes=1)
circulating = False
has_circulated = False
last_ventilation = datetime.now()
ventilate_until = datetime.now() + timedelta(minutes=1)
ventilating = False
has_ventilated = False
start_stage = datetime.now() - timedelta(minutes=1)
delay_stage = datetime.now()
shower_vent = False
status = "loading"
client = None
running = True

heat = 4
ac = 22
fan = 6
sensor_pin = 17

heat_state = False
ac_state = False
fan_state = False
whf_state = False

low = GPIO.HIGH
high = GPIO.LOW

GPIO.setmode(GPIO.BCM)
GPIO.setup(heat, GPIO.OUT)
GPIO.setup(ac, GPIO.OUT)
GPIO.setup(fan, GPIO.OUT)

def log(message):
    if type(message) is not type(""):
        message = json.dumps(message)
    timestamp = datetime.now().strftime("%m/%d/%Y, %H:%M:%S")
    logfiledate = datetime.now().strftime("%Y%m%d")
    logfile = log_loc+"thermostat_"+logfiledate+".log"
    entry = timestamp + ": " + message + "\n"
    print(entry)
    if file_logging is True:
        if os.path.exists(logfile):
            append_write = 'a' # append if already exists
        else:
            append_write = 'w' # make a new file if not

        with open(logfile, append_write) as write_file:
            write_file.write(entry)

def mosquittoDo(topic, command):
    global client
    try:
        client.publish(topic,command)
    except:
        print('failed')

def loop():
    while running is True:
        try:
            cycle()
        except:
            print("BAD CYCLE!!!")
        time.sleep(10)

def read_sensor():
    global temperature
    global humidity
    global failed_reads

    humidity, temperature = Adafruit_DHT.read_retry(Adafruit_DHT.AM2302, sensor_pin)
    while temperature is None and failed_reads < settings["failed_read_halt_limit"]:
        humidity, temperature = Adafruit_DHT.read_retry(Adafruit_DHT.AM2302, sensor_pin)
        if temperature is None:
            failed_reads = failed_reads + 1
            time.sleep(1)
    if temperature is not None:
        temperature = temperature * 9/5.0 + 32

    if humidity is not None and temperature is not None:
        print('Temp={0:0.1f}*  Humidity={1:0.1f}%'.format(temperature, humidity))
    
    if humidity is None:
        humidity = 0
    
    if temperature is None:
        log("sensor failed")
    
    failed_reads = 0

def set_circuit(circuit_pin, state):
    if state is True:
        GPIO.output(circuit_pin, high)
    else:
        GPIO.output(circuit_pin, low)

def heat_off():
    global heat_state
    global last_circulation
    log("heat_off")
    if heat_state is True:
        last_circulation = datetime.now()
    set_circuit(heat, False)
    heat_state = False
    report()

def ac_off():
    global ac_state
    global last_circulation
    global has_circulated
    log("ac_off")
    if ac_state is True:
        last_circulation = datetime.now()
    set_circuit(ac, False)
    ac_state = False
    has_circulated = False
    report()

def fan_off():
    global fan_state
    global last_circulation
    log("fan_off")
    if fan_state is True:
        last_circulation = datetime.now()
    set_circuit(fan, False)
    fan_state = False
    report()

def heat_on():
    global heat_state
    log("heat_on")
    if ac_state is True or fan_state is True:
        return
    set_circuit(heat, True)
    heat_state = True
    report()

def ac_on():
    global ac_state
    log("ac_on")
    if heat_state is True or fan_state is True:
        return
    set_circuit(ac, True)
    ac_state = True
    report()

def fan_on():
    global fan_state
    log("fan_on")
    if ac_state is True or heat_state is True:
        return
    set_circuit(fan, True)
    fan_state = True
    report()

def whf_on():
    global whf_state
    global shower_vent
    global status
    log("whf_on")
    whf_state = True
    sendCommand('turn on whole house fan')
    for evc in extra_ventilation_circuits:
        sendCommand('turn on '+evc)
    more = temperature is not None and temperature > settings["temperature_high_setting"] + 3
    if more is True:
        status = "assisted_ventilation"
        sendCommand('turn on shower fan')
        shower_vent = True
    report()

def whf_off():
    global whf_state
    global shower_vent
    log("whf_off")
    whf_state = False
    sendCommand('turn off whole house fan')
    for evc in extra_ventilation_circuits:
        sendCommand('turn off '+evc)
    if shower_vent is True:
        sendCommand('turn off shower fan')
    shower_vent = False
    report()

def halt():
    global fan_state
    global heat_state
    global ac_state
    global whf_state
    log("halt")
    GPIO.output(heat, low)
    GPIO.output(ac, low)
    GPIO.output(fan, low)
    sendCommand('turn off whole house fan')
    sendCommand('turn off circulating fan')
    sendCommand('turn off floor fan')
    fan_state = False
    heat_state = False
    ac_state = False
    whf_state = False
    report()

def cycle():
    global last_circulation
    global status

    read_sensor()
    if temperature is None or temperature == 0:
        status = "sensor_fail"

    if temperature is None:
        halt()
        status = "halted"
        return

    if circulating is True:
        if datetime.now() > circulate_until:
            stop_circulating()
        else:
            return

    if ventilating is True:
        if datetime.now() > ventilate_until:
            stop_ventilating()
        else:
            return

    if delay_stage > datetime.now():
        status = "delayed"
        return

    if settings["system_disabled"] > 0:
        status = "disabled"
        if ac_state is True:
            ac_off()
        if heat_state is True:
            heat_off()
        if circulating is True:
            stop_circulating()
        if ventilating is True:
            stop_ventilating()
        return
    
    if round(temperature) > settings["temperature_high_setting"] and ac_state is False:
        cool_down()
        return
    
    if round(temperature) > settings["temperature_high_setting"] - settings["swing_temp_offset"] and ac_state is True: # cool beyond the on limit
        cool_down()
        return
    
    if round(temperature) < settings["temperature_low_setting"] and heat_state is False:
        warm_up()
        return
    
    if round(temperature) < settings["temperature_low_setting"] + settings["swing_temp_offset"] and heat_state is True: # heat beyond the on limit
        warm_up()
        return
    
    if heat_state is True:
        heat_off()
    
    if ac_state is True:
        ac_off()

    if settings["air_circulation_minutes"] > 0 and datetime.now() > last_circulation + timedelta(minutes=settings["air_circulation_minutes"]):
        circulate_air(settings["circulation_cycle_minutes"])
        return
        
    status = "stand_by"

def cool_down():
    global start_stage
    global delay_stage
    global has_ventilated
    global status
    if ac_state is True:
        if start_stage < datetime.now() - timedelta(minutes=settings["stage_limit_minutes"]):
            delay_stage = datetime.now() + timedelta(minutes=settings["stage_cooldown_minutes"])
            ac_off()
        return
    # if round(humidity) > settings["humidity_setting"] and settings["humidity_setting"] > 0 and has_circulated is False:
    #     circulate_air(settings["circulation_cycle_minutes"])
    if temperature > settings["temperature_high_setting"] + 2 and has_ventilated is False:
        ventilate_air(settings["ventilation_cycle_minutes"])
    has_ventilated = False
    start_stage = datetime.now()
    ac_on()
    status = "cooling"

def warm_up():
    global start_stage
    global delay_stage
    global status
    if heat_state is True:
        if start_stage < datetime.now() - timedelta(minutes=settings["stage_limit_minutes"]):
            delay_stage = datetime.now() + timedelta(minutes=settings["stage_cooldown_minutes"])
            heat_off()
        return
    start_stage = datetime.now()
    heat_on()
    status = "heating"

def circulate_air(minutes):
    global circulate_until
    global circulating
    global status
    if circulating is True:
        return
    fan_on()
    circulate_until = datetime.now() + timedelta(minutes=minutes) 
    circulating = True
    status = "circulating"

def ventilate_air(minutes):
    global ventilate_until
    global ventilating
    global status
    if ventilating is True:
        return
    if settings["use_whole_house_fan"] > 0:
        whf_on()
    ventilate_until = datetime.now() + timedelta(minutes=minutes) 
    ventilating = True
    status = "ventilating"

def stop_circulating():
    global circulating
    global has_circulated
    global status
    fan_off()
    circulating = False
    has_circulated = True

def stop_ventilating():
    global ventilating
    global has_ventilated
    global status
    if settings["use_whole_house_fan"] > 0:
        whf_off()
    ventilating = False
    has_ventilated = True

def on_message(client, userdata, message):
    global settings
    global running
    try:
        text = str(message.payload.decode("utf-8"))
        if text == "halt":
            running = False
            return
        data = text.split(':')
        settings[data[0]] = int(data[1])
    except Exception as err:
        log("Unexpected error in on_message: "+str(err))

def on_disconnect(client, userdata, rc):
    reconnect()

def reconnect():
    try:
        client.connect(broker_ip)
    except Exception as err:
        log("Unexpected error: "+str(err))
        time.sleep(10)
        reconnect()

def sendCommand(command):
    mosquittoDo("smarter_circuits/command",command)

def report():
    global status
    hum = humidity
    temp = temperature
    #print('Temp={0:0.1f}*  Humidity={1:0.1f}%'.format(temperature, humidity))
    if humidity == None:
        hum = 0
    if temperature == None:
        temp = 0
    cool = "off"
    if ac_state is True:
        cool = "on"
    circ = "off"
    if fan_state is True:
        circ = "on"
    h = "off"
    if heat_state is True:
        h = "on"
    w = "off"
    if whf_state is True:
        w = "on"
    if settings["system_disabled"] > 0:
        status = "disabled"
    report = 'report: {0:0.1f} F {1:0.1f}% AC:{2} Fan:{3} Heat:{4} WHF:{5} Status:{6} Last Start:{7} Last Circ:{8}'.format(temp, hum,cool,circ,h,w,status,start_stage.strftime("%m/%d/%Y, %H:%M:%S"),last_circulation.strftime("%m/%d/%Y, %H:%M:%S"))
    log(report)
    mosquittoDo("smarter_circuits/thermostats/"+room+"/status",report)

if __name__ == "__main__":
    client = mqtt.Client()
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.connect(broker_ip)
    client.subscribe("smarter_circuits/thermostats/"+room+"/command")
    halt()
    loop()
    client.disconnect()