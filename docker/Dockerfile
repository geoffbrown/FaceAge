# FaceAge inference environment — reproduces AIM-Harvard/FaceAge environment-cpu.yaml
#
# Built for linux/amd64 and run under Rosetta on Apple Silicon. The authors'
# environment-cpu.yaml pins linux-64 conda build strings (h06a4308, h7b6447c),
# so it is only usable verbatim inside a linux/amd64 container.
#
# Python 3.6.13 matches the authors' interpreter exactly. The numerically
# relevant packages are pinned to the authors' exact versions; see README
# "Deviations from the authors' environment" for the conda->pip note.

FROM --platform=linux/amd64 python:3.6.13-slim-buster

# Debian buster is EOL and its apt repositories have moved to archive.debian.org.
RUN sed -i \
      -e 's|deb.debian.org/debian|archive.debian.org/debian|g' \
      -e 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' \
      -e '/buster-updates/d' \
      /etc/apt/sources.list \
 && printf 'Acquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99no-check-valid-until \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      libglib2.0-0 libsm6 libxext6 libxrender1 libgl1-mesa-glx \
 && rm -rf /var/lib/apt/lists/*

# Base tooling at the versions from the conda section of environment-cpu.yaml
RUN python -m pip install --no-cache-dir \
      pip==21.2.2 setuptools==58.0.4 wheel==0.37.0

# Exact pip pins from environment-cpu.yaml
RUN pip install --no-cache-dir \
      numpy==1.19.5 \
      h5py==3.1.0 \
      importlib-metadata==4.8.2 \
      keras==2.6.0 \
      keras-preprocessing==1.1.2 \
      mtcnn==0.1.1 \
      opencv-python==4.5.4.60 \
      pandas==1.1.5 \
      scikit-image \
      seaborn==0.11.2 \
      tensorboard==2.6.0 \
      tensorboard-data-server==0.6.1 \
      tensorboard-plugin-wit==1.8.0 \
      tensorflow-estimator==2.6.0 \
      tensorflow==2.6.2 \
      pyyaml==5.4.1 \
      python-dateutil==2.8.2

# environment-cpu.yaml installs six 1.16.0 through conda, which never sees pip's
# "six~=1.15.0" constraint from tensorflow 2.6.2 (conda and pip resolve
# independently). Installing it with --no-deps reproduces the authors' actual
# runtime state rather than silently downgrading six.
RUN pip install --no-cache-dir --no-deps six==1.16.0

# TensorFlow 2.6 spawns threads per physical core; keep it predictable.
ENV TF_CPP_MIN_LOG_LEVEL=3 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0

WORKDIR /work
COPY src/faceage_run.py /opt/faceage/faceage_run.py

ENTRYPOINT ["python", "-W", "ignore", "/opt/faceage/faceage_run.py"]
