# Installation Guide

## Prerequisites

- Python 3.8 or higher
- EPICS Base installed
- Environment variables set:
  - `EPICS_BASE`
  - `EPICS_HOST_ARCH`
  - EPICS binaries in PATH

## Step 1: Install Python Dependencies

```bash
pip install -r requirements.txt
```

## Step 2: Install infn_ophyd_hal

The `infn_ophyd_hal` package provides Ophyd device implementations for INFN beamline devices.

### Option A: Install from local directory (if you have the source)

```bash
# Navigate to infn_ophyd_hal directory
cd /path/to/infn_ophyd_hal

# Install in development mode
pip install -e .

# Or install normally
pip install .
```

### Option B: Add to Python path

If you don't want to install it, add it to your Python path:

```bash
export PYTHONPATH="/path/to/infn_ophyd_hal:$PYTHONPATH"
```

Or add it in your script/application startup.

### Option C: Install from git (if available)

```bash
pip install git+https://github.com/infn-epics/infn_ophyd_hal.git
```

## Step 3: Verify Installation

```python
# Test import
python -c "from infn_ophyd_hal import OphydTmlMotor; print('Success!')"
```

## Step 4: Configure Your Beamline

Edit `values.yaml` with your beamline configuration:

```yaml
beamline: sparc
namespace: sparc

epicsConfiguration:
  iocs:
    - name: "tml-ch1"
      devgroup: "mag"      # Required for Ophyd device creation
      devtype: "tml"       # Required for Ophyd device creation
      iocprefix: "TML-CH1"
      # ... rest of configuration
```

## Step 5: Run the Controller

```bash
iocmng-server

# or use the explicit standalone alias
iocmng-standalone
```

For standalone plugin loading, point `IOCMNG_PLUGINS_CONFIG` at a YAML file whose entries use either `git_url` or `local_path`.

## Troubleshooting

### Import Error: No module named 'infn_ophyd_hal'

**Solution**: Make sure you've installed or added the package to your Python path.

```bash
# Check if installed
pip list | grep infn

# Or check Python path
python -c "import sys; print('\n'.join(sys.path))"
```

### Ophyd Device Creation Warnings

If you see warnings like:
```
WARNING: No Ophyd class registered for mag/tml, device tml-ch1 will not be created
```

**Solution**: 
1. Check that `infn_ophyd_hal` is properly installed
2. Verify the `devgroup` and `devtype` match supported types
3. Check the factory registration in `ophyd_device_factory.py`

### EPICS Connection Issues

**Solution**: Set EPICS environment variables:

```bash
export EPICS_CA_ADDR_LIST="192.168.1.255"  # Your network broadcast address
export EPICS_CA_AUTO_ADDR_LIST=NO
```

## Running Without Ophyd Devices

If you don't need Ophyd device integration, the controller will still work:

1. Tasks without Ophyd devices will function normally
2. You'll see warnings about devices not being created
3. All soft IOC PVs will still be created and accessible

## Development Setup

For development, install additional tools:

```bash
pip install pytest black flake8 mypy
```

Run tests:
```bash
pytest tests/
```

Format code:
```bash
black .
flake8 .
```
