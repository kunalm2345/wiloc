idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
)

# Allow setting device ID at build time: idf.py build -DWILOC_DEVICE_ID=anchor_03
if(DEFINED WILOC_DEVICE_ID)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE CONFIG_WILOC_DEVICE_ID="${WILOC_DEVICE_ID}")
endif()
