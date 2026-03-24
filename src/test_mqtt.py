import paho.mqtt.client as mqtt

client = mqtt.Client()
client.connect("192.168.4.42", 1883, 60) # Connects to your Docker broker
client.publish("home/front_door/motion", "on") # Shouts "motion detected!"

print("Fake motion trigger sent to MQTT broker!")