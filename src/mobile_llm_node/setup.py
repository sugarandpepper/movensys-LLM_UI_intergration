from setuptools import find_packages, setup

package_name = 'mobile_llm_node'

setup(
    name=package_name,
    version='0.0.1',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='woojin',
    maintainer_email='ww9762ww@gmail.com',
    description="Real LLM-side node backed by Mobile_LLM's chat_v2 model",
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'mobile_llm_node = mobile_llm_node.chat_v2_node:main',
        ],
    },
)
