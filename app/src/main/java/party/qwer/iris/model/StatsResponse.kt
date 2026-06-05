package party.qwer.iris.model

import kotlinx.serialization.Serializable

@Serializable
data class StatsResponse(
    val roomStats: List<Map<String, String?>>? = null,
    val hourlyUsage: List<Map<String, String?>>? = null,
    val userStats: List<Map<String, String?>>? = null,
)
