def pytest_addoption(parser):
    parser.addoption("--integration", action="store_true", default=False,
                     help="Explicitly opt into isolated local PostgreSQL integration checks")
