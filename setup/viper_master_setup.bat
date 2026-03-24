@echo off
set "ROOT=C:\viper_vision"

:: 1. Check for Admin rights
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo ERROR: You MUST right-click and 'Run as Administrator'.
    pause
    exit
)

echo --- Phase 1: Creating Directory Structure ---
if not exist "%ROOT%" mkdir "%ROOT%"
if not exist "%ROOT%\mosquitto\config" mkdir "%ROOT%\mosquitto\config"
if not exist "%ROOT%\mosquitto\data" mkdir "%ROOT%\mosquitto\data"
if not exist "%ROOT%\mosquitto\log" mkdir "%ROOT%\mosquitto\log"
if not exist "%ROOT%\hass_config" mkdir "%ROOT%\hass_config"
if not exist "%ROOT%\ring_data" mkdir "%ROOT%\ring_data"

echo --- Phase 2: Writing Mosquitto Configuration ---
(
echo # --- Network Listeners ---
echo listener 1883 0.0.0.0
echo.
echo # --- Security Settings ---
echo allow_anonymous true
echo.
echo # --- Persistence ---
echo persistence true
echo persistence_location /mosquitto/data/
echo log_dest file /mosquitto/log/mosquitto.log
) > "%ROOT%\mosquitto\config\mosquitto.conf"

echo --- Phase 3: Fixing Windows Port Conflicts (WinNAT) ---
echo Stopping WinNAT to release ports...
net stop winnat

echo Reserving Ports: 1883 (MQTT), 8123 (HASS), 8554 (RTSP), 55123 (Web UI)
netsh int ipv4 add excludedportrange protocol=tcp startport=1883 numberofports=1
netsh int ipv4 add excludedportrange protocol=tcp startport=8123 numberofports=1
netsh int ipv4 add excludedportrange protocol=tcp startport=8554 numberofports=1
netsh int ipv4 add excludedportrange protocol=tcp startport=55123 numberofports=1

echo --- Phase 4: Opening Windows Firewall ---
netsh advfirewall firewall add rule name="Viper_RTSP" dir=in action=allow protocol=TCP localport=8554
netsh advfirewall firewall add rule name="Viper_HASS" dir=in action=allow protocol=TCP localport=8123
netsh advfirewall firewall add rule name="Viper_RingWeb" dir=in action=allow protocol=TCP localport=55123

echo Restarting WinNAT...
net start winnat

echo --- Phase 5: Launching Docker Stack ---
cd /d "%ROOT%"
if exist "docker-compose.yml" (
    docker-compose up -d
    echo SUCCESS: Viper Vision stack is launching.
) else (
    echo WARNING: docker-compose.yml not found in %ROOT%. 
    echo Please drop your YAML file there and run 'docker-compose up -d' manually.
)

echo.
echo --- INSTALL COMPLETE ---
echo You can now access your Ring Login at http://localhost:55123
pause