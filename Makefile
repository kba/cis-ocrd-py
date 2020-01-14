PY ?= python3
PIP ?= pip3

GITURL ?= "https://github.com/cisocrgroup"
PREFIX ?= /usr/local
BINDIR ?= $(PREFIX)/bin

install:
	${PIP} install --upgrade pip .
install-devel:
	${PIP} install --upgrade pip -e .

docker-build: Dockerfile
	docker build -t flobar/ocrd_cis:latest .
docker-push: docker-build
	docker push flobar/ocrd_cis:latest

TEST_SCRIPTS=$(wildcard tests/run_*.sh)
.PHONY: $(TEST_SCRIPTS)
$(TEST_SCRIPTS):
	bash $@
# run test scripts
test: $(TEST_SCRIPTS)


build-profiler: PROFILER_BUILD_DIR := $(shell mktemp -d)
build-profiler:
	rm -rf $(PROFILER_BUILD_DIR) \
	&& git clone ${GITURL}/Profiler --branch devel --single-branch $(PROFILER_BUILD_DIR) \
	&& cd $(PROFILER_BUILD_DIR) \
	&& mkdir build \
	&& cd build \
	&& cmake -DCMAKE_BUILD_TYPE=release .. \
	&& make compileFBDic trainFrequencyList profiler \
	&& cp bin/compileFBDic bin/trainFrequencyList bin/profiler $(BINDIR) \
	&& cd / \
	&& rm -rf $(PROFILER_BUILD_DIR)

.PHONY: install test build-profiler
